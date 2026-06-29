"""Enrich aid centers: normalize needs -> taxonomy, classify type, geocode, set freshness.

Makes the resource side queryable like the person side:
  * needs_tags : controlled vocabulary so "needs insulin" -> tag `medicinas` reliably
  * ctype      : acopio | refugio | hub
  * lat/lng    : geocoded (Nominatim/OSM) for "near me"
  * status/last_confirmed : freshness (default active/now; updated by crowd-confirm)
Deterministic (no LLM) so it's consistent + cheap. Geocoding is throttled to OSM's 1 req/s.
"""
from __future__ import annotations

import json, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import store_db as db   # noqa: E402
import names as nm      # noqa: E402

# controlled vocabulary: tag -> keyword fragments (folded match)
TAXONOMY = {
    "agua": ["agua"],
    "alimentos": ["aliment", "comida", "perecedero", "viver", "enlatad", "mercado", "granos"],
    "medicinas": ["medicina", "medicament", "farmac", "insulina", "tratamiento", "antibiotic"],
    "insumos_medicos": ["insumo", "material medico", "gasa", "jeringa", "quirurg", "suero", "vendaje", "guantes"],
    "higiene": ["higiene", "jabon", "toalla", "aseo", "cepillo", "desinfect", "cloro", "papel"],
    "panales": ["panal", "toallas sanitarias", "femenin"],
    "bebes": ["bebe", "formula", "leche", "lactante"],
    "ropa": ["ropa", "abrigo", "calzado", "zapato", "vestiment"],
    "colchones_cobijas": ["colchon", "cobija", "manta", "sabana", "almohada", "cama"],
    "refugio": ["refugio", "albergue", "alojamiento", "carpa"],
    "herramientas": ["herramienta", "pala", "pico", "saco", "cuerda", "marcador", "linterna", "bota"],
    "combustible": ["gasolina", "combustible", "planta electric", "generador", "diesel", "gasoil"],
    "voluntarios": ["voluntari"],
    "dinero": ["dinero", "monetar", "efectivo", "transferencia", "zelle", "donacion en"],
    "mascotas": ["mascota", "perro", "gato", "animal", "veterinar"],
}
_TAX = {tag: [nm._fold(k) for k in kws] for tag, kws in TAXONOMY.items()}


def normalize_needs(text: str) -> list[str]:
    t = nm._fold(text)
    return [tag for tag, kws in _TAX.items() if any(k in t for k in kws)]


def classify_ctype(name: str, needs: str, ctype: str) -> str:
    t = nm._fold(f"{name} {needs} {ctype}")
    if "refugio" in t or "albergue" in t or "damnificad" in t:
        return "refugio"
    if "almacen" in t or "distribu" in t or "logistic" in t:
        return "hub"
    return "acopio"


def geocode(query: str, country: str | None = "ve") -> tuple[float | None, float | None]:
    params = {"q": query, "format": "json", "limit": 1}
    if country:
        params["countrycodes"] = country
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "directorio-sismo/1.0 (humanitarian reunification)"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None, None


def geocode_center(address: str, municipality: str) -> tuple[float | None, float | None]:
    """3-step: full address in VE -> municipality in VE -> worldwide (diaspora drop-offs)."""
    q = ", ".join(x for x in [address, municipality] if x) or (municipality or "")
    if not q:
        return None, None
    lat, lng = geocode(q + ", Venezuela")
    if lat is None and municipality:
        lat, lng = geocode(municipality + ", Venezuela")
    if lat is None and municipality:
        lat, lng = geocode(municipality, country=None)
    return lat, lng


def enrich(conn, do_geocode: bool = True, geo_pause: float = 1.1) -> dict:
    db.ensure_center_columns(conn)
    rows = conn.execute("SELECT center_id,name,address,municipality,needs,ctype,lat,status FROM centers").fetchall()
    tagged = geocoded = 0
    for r in rows:
        tags = normalize_needs(r["needs"] or "")
        ctype = classify_ctype(r["name"] or "", r["needs"] or "", r["ctype"] or "")
        conn.execute("""UPDATE centers SET needs_tags=?, ctype=?,
                        status=COALESCE(NULLIF(status,''),'active'),
                        last_confirmed=COALESCE(last_confirmed, ?) WHERE center_id=?""",
                     (json.dumps(tags), ctype, db._now(), r["center_id"]))
        tagged += 1
        if do_geocode and r["lat"] is None:
            lat, lng = geocode_center(r["address"] or "", r["municipality"] or "")
            if lat is not None:
                conn.execute("UPDATE centers SET lat=?, lng=? WHERE center_id=?", (lat, lng, r["center_id"]))
                geocoded += 1
            time.sleep(geo_pause)   # respect OSM 1 req/s
        # Persist THIS row before the next (slow, fail-prone) network call: a crash
        # loses at most one row, the geocoded work already done stays, and a re-run
        # resumes (geocode is gated on lat IS NULL). Also frees the write lock so the
        # maintainer isn't blocked for the whole batch.
        conn.commit()
    return {"tagged": tagged, "geocoded": geocoded, "total": len(rows)}


if __name__ == "__main__":
    print(enrich(db.connect()))
