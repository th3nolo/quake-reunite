"""Central de-dup REST API (FastAPI) — SQLite-backed.

Reads the resolved registry live from the SQLite store (WAL: concurrent with the
maintainer's writes). No giant JSON held in RAM. Read-only. "Show everything"
policy (full cédula + status), with per-IP rate limit + audit log so the API
can't be drained as a bulk PII dump.

  uvicorn api.app:app --host 0.0.0.0 --port 8080
DB via DB_PATH / DATA_DIR.
"""
from __future__ import annotations

import json, math, os, sqlite3, sys, time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.middleware.gzip import GZipMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # api dir (for ask)
import store_db as db  # noqa: E402
import ask             # noqa: E402  (NL consultation layer)
import households as _hh  # noqa: E402
import geo_people as _geo  # noqa: E402  (approx zone coords for people)
import normalize as nz     # noqa: E402  (fold = same accent/case folding as name_key)

try:
    _geo._load_footprints()   # parse footprint centroids once at startup, not on first request
except Exception:
    pass

RATE_PER_MIN = int(os.environ.get("API_RATE_PER_MIN", "120"))
AUDIT_LOG = Path(os.environ.get("AUDIT_LOG", Path(db.DB_PATH).parent / "api_audit.log"))

app = FastAPI(title="Directorio Sismo 2026 — de-dup API", version="2.0 (sqlite)")
app.add_middleware(GZipMiddleware, minimum_size=1000)   # ~18MB search page -> ~2MB on the wire
_hits: dict[str, deque] = defaultdict(deque)

WEB_DIR = Path(os.environ.get("WEB_DIR", Path(db.DB_PATH).parent))   # serves buscador.html next to the DB


APP_ROOT = Path(__file__).resolve().parent.parent


@app.get("/", include_in_schema=False)
def home():
    # Landing page = 3D affected-buildings map (baked into the image). The self-contained
    # search/list page now lives at /buscador.
    f = APP_ROOT / "web" / "mapa3d.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>mapa3d.html no encontrado</h1>", status_code=503)


@app.get("/buscador", include_in_schema=False)
def buscador():
    f = WEB_DIR / "buscador.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>Buscador no generado aún</h1><p>run pipeline/build_db.py</p>", status_code=503)


@app.get("/mapa", include_in_schema=False)
def mapa():
    f = APP_ROOT / "web" / "mapa.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>mapa.html no encontrado</h1>", status_code=503)


@app.get("/mapa3d", include_in_schema=False)
def mapa3d():
    f = APP_ROOT / "web" / "mapa3d.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>mapa3d.html no encontrado</h1>", status_code=503)


@app.get("/data/edificios.geojson", include_in_schema=False)
def edificios():
    """Affected-building footprints (Microsoft AI for Good Lab / R. Franco), gzipped by middleware."""
    f = WEB_DIR / "edificios_afectados.geojson"
    if f.exists():
        return FileResponse(str(f), media_type="application/geo+json")
    return JSONResponse({"type": "FeatureCollection", "features": []})


_ZONE_AGG = {"ts": 0.0, "data": None}


def _primary_status(sts: list) -> str:
    for s in ("fallecido", "localizado", "ingresado", "herido", "alta", "por_localizar"):
        if s in sts:
            return s
    return sts[0] if sts else "?"


@app.get("/map/zones")
def map_zones():
    """Overview for the default map view: per-zone people counts by status. Cached 120s."""
    now = time.time()
    if _ZONE_AGG["data"] is not None and now - _ZONE_AGG["ts"] < 120:
        return _ZONE_AGG["data"]
    c = _conn()
    try:
        zones = _zones(c)
        coord = {zf: (lat, lng) for zf, lat, lng in zones}
        agg: dict = defaultdict(lambda: defaultdict(int))
        for r in c.execute("SELECT origins, statuses FROM persons"):
            loc = _geo.locate(json.loads(r["origins"] or "[]"), zones)
            if not loc:
                continue
            agg[loc["zone"]][_primary_status(json.loads(r["statuses"] or "[]"))] += 1
        out = []
        for zf, by in agg.items():
            lat, lng = coord.get(zf, (None, None))
            if lat is None:
                continue
            out.append({"zone": zf, "lat": lat, "lng": lng,
                        "total": sum(by.values()), "by_status": dict(by)})
        out.sort(key=lambda z: z["total"], reverse=True)
        data = {"zones": out}
        _ZONE_AGG.update(ts=now, data=data)
        return data
    finally:
        c.close()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(db.DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    return c


_ZONES = None


def _zones(conn):
    global _ZONES
    if _ZONES is None:
        _ZONES = _geo.load_zones(conn)
    return _ZONES


def _client_ip(request: Request) -> str:
    """Real client IP behind Traefik. Trust X-Real-Ip first (Traefik overwrites it
    with the direct peer, so a client can't spoof it), then the first X-Forwarded-For
    hop, then the socket peer. The container is only reachable via Traefik (no published
    port), so these headers are trustworthy here. Without this, the rate-limit + audit
    guard keys on Traefik's IP and the anti-bulk-PII control is defeated."""
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


@app.middleware("http")
async def _ratelimit_audit(request: Request, call_next):
    ip = _client_ip(request)
    now = time.time()
    dq = _hits[ip]
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= RATE_PER_MIN:
        return JSONResponse({"error": "rate_limited", "retry_after_s": 60}, status_code=429)
    dq.append(now)
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(f"{time.strftime('%FT%TZ', time.gmtime())}\t{ip}\t{request.method}\t{request.url.path}?{request.url.query}\n")
    except Exception:
        pass
    return await call_next(request)


@app.get("/health")
def health():
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        m = c.execute("SELECT COUNT(*) FROM centers").fetchone()[0]
        c.close()
        return {"ok": True, "persons": n, "centers": m, "db": db.DB_PATH}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/stats")
def stats():
    c = _conn()
    try:
        return db.stats(c)
    finally:
        c.close()


@app.get("/stats/history")
def stats_history(limit: int = Query(500, ge=1, le=5000)):
    """Durable per-cycle snapshots + the net change across the window (newly found,
    newly hospitalized, newly missing) — observability over what the sync actually changed."""
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cycle_metrics ORDER BY ts DESC LIMIT ?", (limit,))]
    except sqlite3.OperationalError:
        rows = []
    finally:
        c.close()
    change = {}
    if len(rows) >= 2:
        new, old = rows[0], rows[-1]
        for k in ("persons", "records", "localizado", "ingresado", "por_localizar", "deceased", "centers"):
            change[k] = new.get(k, 0) - old.get(k, 0)
        change.update(desde=old["ts"], hasta=new["ts"], ciclos=len(rows))
    return {"change_over_window": change, "cycles": rows}


@app.get("/salud", include_in_schema=False)
def salud():
    f = APP_ROOT / "web" / "salud.html"
    return FileResponse(str(f), media_type="text/html") if f.exists() else HTMLResponse("salud.html missing", 503)


@app.get("/persons")
def persons(status: str | None = None, name: str | None = None, ci: str | None = None,
            municipality: str | None = None, limit: int = Query(50, ge=1, le=200)):
    where, args = [], []
    if ci:
        digits = "".join(ch for ch in ci if ch.isdigit())
        where.append("person_id IN (SELECT person_id FROM person_ci WHERE ci=?)")
        args.append(digits)
    if status:
        where.append("statuses LIKE ?")
        args.append(f'%"{status}"%')
    if municipality:
        where.append("(origins LIKE ? OR hospitals LIKE ?)")
        args += [f"%{municipality}%", f"%{municipality}%"]
    if name:
        for tok in nz.fold(name).split():   # fold so accented queries match folded name_key
            where.append("name_key LIKE ?")
            args.append(f"%{tok}%")
    sql = "SELECT * FROM persons"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY n_records DESC LIMIT ?"
    args.append(limit)
    c = _conn()
    try:
        rows = c.execute(sql, args).fetchall()
        total = c.execute("SELECT COUNT(*) FROM (" + sql.replace(" ORDER BY n_records DESC LIMIT ?", "") + ")",
                          args[:-1]).fetchone()[0]
        results = [db.person_to_dict(r) for r in rows]
        _geo.attach(c, results, _zones(c), max_live=0)   # cache+gazetteer only — no live geocode in the request
        return {"count": total, "results": results}
    finally:
        c.close()


@app.get("/persons/{ci}")
def person_by_ci(ci: str):
    digits = "".join(ch for ch in ci if ch.isdigit())
    c = _conn()
    try:
        row = c.execute("""SELECT p.* FROM persons p JOIN person_ci pc ON pc.person_id=p.person_id
                           WHERE pc.ci=? LIMIT 1""", (digits,)).fetchone()
        if not row:
            return JSONResponse({"error": "not_found", "ci": digits}, status_code=404)
        d = db.person_to_dict(row)
        d["appearances"] = [dict(r) for r in c.execute(
            "SELECT source,source_type,hospital,ci,age,sex,origin,status,date,obs FROM records WHERE person_id=?",
            (row["person_id"],))]
        d["household"] = _hh.members(c, row["person_id"])   # probable family/household links
        return d
    finally:
        c.close()


def _haversine(la1, lo1, la2, lo2):
    r = 6371.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = math.radians(la2 - la1), math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def _freshness(iso):
    if not iso:
        return "sin confirmar"
    try:
        import calendar
        age = time.time() - calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
        h = age / 3600
        return f"confirmado hace {int(h)}h" if h < 48 else f"sin confirmar hace {int(h/24)}d"
    except Exception:
        return "sin confirmar"


@app.get("/centers")
def centers(q: str | None = None, ctype: str | None = None, municipality: str | None = None,
            need: str | None = None, status: str | None = None, near: str | None = None,
            radius_km: float = 10.0, limit: int = Query(100, ge=1, le=500)):
    where, args = ["canonical_id IS NULL"], []   # hide rows merged into a canonical center
    if ctype:
        where.append("ctype=?"); args.append(ctype)
    if status:
        where.append("status=?"); args.append(status)
    if municipality:
        where.append("municipality LIKE ?"); args.append(f"%{municipality}%")
    if need:                                  # normalized taxonomy tag OR raw text
        where.append("(needs_tags LIKE ? OR needs LIKE ?)"); args += [f'%"{need}"%', f"%{need}%"]
    if q:
        where.append("(name LIKE ? OR address LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
    sql = ("SELECT center_id,name,ctype,address,municipality,hours,needs,needs_tags,status,"
           "last_confirmed,phone,operator,source,merged_sources,url,lat,lng FROM centers")
    if where:
        sql += " WHERE " + " AND ".join(where)
    c = _conn()
    try:
        rows = [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()
    for r in rows:
        try:
            r["needs_tags"] = json.loads(r.get("needs_tags") or "[]")
        except Exception:
            r["needs_tags"] = []
        r["freshness"] = _freshness(r.get("last_confirmed"))
    if near:                                  # filter + sort by distance
        try:
            la, lo = [float(x) for x in near.split(",")]
            for r in rows:
                r["dist_km"] = _haversine(la, lo, r["lat"], r["lng"]) if r.get("lat") is not None else None
            rows = sorted((r for r in rows if r.get("dist_km") is not None and r["dist_km"] <= radius_km),
                          key=lambda r: r["dist_km"])
        except Exception:
            pass
    return {"count": len(rows), "results": rows[:limit]}


# Center freshness is AGENT-driven, not human: the Gemma agent re-reads each center's
# source page (centers_refresh) on demand and via the maintainer loop. No public POST
# confirm endpoint — the data is maintained by re-reading the source, not by people.


@app.get("/review")
def review(limit: int = Query(100, ge=1, le=500)):
    c = _conn()
    try:
        rows = c.execute("""SELECT * FROM persons WHERE ci_conflict=1 OR (only_name_merge=1 AND n_records>1)
                            ORDER BY n_records DESC LIMIT ?""", (limit,)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM persons WHERE ci_conflict=1 OR (only_name_merge=1 AND n_records>1)").fetchone()[0]
        return {"count": total, "results": [db.person_to_dict(r) for r in rows]}
    finally:
        c.close()


@app.get("/ask")
def ask_endpoint(q: str = Query(..., min_length=2, description="Pregunta en lenguaje natural")):
    """NL question -> Gemma plans a query + consults external sources -> grounded answer.
    Person results get approx zone lat/lng so the map can plot them."""
    res = ask.answer(q)
    if res.get("results") and (res.get("query") or {}).get("tool") != "centers":
        c = _conn()
        try:
            _geo.attach(c, res["results"], _zones(c), max_live=0)   # no live geocode in the request path
        finally:
            c.close()
    return res


@app.post("/admin/reload")
def reload_():
    # DB-backed: queries are always live, nothing to reload.
    return {"ok": True, "note": "sqlite-backed; reads are live, no reload needed"}
