"""Approximate, HONEST geolocation for people via their free-text origin.

People have no coordinates — only messy free-text origins ("Playa Grande, La Guaira",
"Catia la mar edf Belo Horizonte"), and almost everyone clusters in the quake zone
(La Guaira / Vargas / Caracas). There is no per-person precision to be had for free, so
we map each origin to the nearest KNOWN zone centroid by substring match. That is the
real granularity, plotted as clusters. Centers, separately, ARE precisely geocoded.

Zone centroids are geocoded once (Nominatim) and cached in the `origin_zones` table.
Matching prefers the LONGEST zone name found in the origin so "catia la mar" wins over
the bare "guaira"/"caracas".
"""
from __future__ import annotations

import json, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
import store_db as db        # noqa: E402
import names as nm           # noqa: E402
import centers_enrich as ce  # noqa: E402

# Quake-zone places + the cities our centers sit in. Geocoded once, then matched by
# substring against each person's folded origin. Order doesn't matter (we sort by length).
# CURATED coords for the quake-zone sectors + the cities our centers sit in. Hardcoded
# (not auto-geocoded) because free geocoders confidently return wrong-state homonyms
# ("Playa Grande" -> Sucre). These are sector-level centroids (~town/sector precision).
ZONE_COORDS = {
    "Catia La Mar": (10.598, -67.020), "Playa Grande": (10.607, -67.045),
    "Maiquetía": (10.602, -66.981), "La Guaira": (10.601, -66.931),
    "Macuto": (10.606, -66.892), "Caraballeda": (10.611, -66.852),
    "Los Corales": (10.613, -66.857), "El Caribe": (10.610, -66.846), "Caribe": (10.610, -66.846),
    "Tanaguarena": (10.617, -66.818), "Naiguatá": (10.618, -66.741), "Camurí Grande": (10.610, -66.700),
    "Carayaca": (10.558, -67.130), "La Sabana": (10.632, -66.395), "Vargas": (10.601, -66.931),
    # aliases + sectors the Gemma audit found missing:
    "Guaira": (10.601, -66.931), "La Guira": (10.601, -66.931), "Los Cocos": (10.612, -66.858),
    "Playa Verde": (10.606, -67.052), "Los Caracas": (10.617, -66.563), "Anare": (10.620, -66.611),
    # round 2 (place-landing audit): more La Guaira/Vargas sectors + spelling variants
    "La Guaria": (10.601, -66.931), "La Llanada": (10.601, -66.931), "Montesano": (10.598, -66.945),
    "Pariata": (10.602, -66.975), "Mare Abajo": (10.600, -66.985), "Aeropuerto": (10.601, -66.991),
    "Catita": (10.598, -67.020), "Cata La Mar": (10.598, -67.020), "Corales": (10.613, -66.857),
    "Chuspa": (10.658, -66.315),
    # In THIS quake "Catia" = Catia La Mar (affected coast), NOT Caracas's Catia parish.
    # Point bare "catia"/"catialamar"/"catia lamar" to Catia La Mar so they land in the zone.
    "Caracas": (10.488, -66.879), "Catia": (10.598, -67.020), "Petare": (10.477, -66.808),
    "Maracay": (10.247, -67.596), "Turmero": (10.228, -67.474), "Barquisimeto": (10.067, -69.322),
    "Maracaibo": (10.654, -71.640), "Mérida": (8.589, -71.144), "Valencia": (10.162, -67.998),
    "Barinas": (8.623, -70.207), "Barcelona": (10.135, -64.688), "San Cristóbal": (7.767, -72.225),
    "Lechería": (10.183, -64.690),
}


def ensure_zones(conn, pause: float = 0.0) -> dict:
    """Load the curated sector centroids (deterministic, no network)."""
    conn.execute("CREATE TABLE IF NOT EXISTS origin_zones(zone TEXT PRIMARY KEY, lat REAL, lng REAL)")
    conn.execute("DELETE FROM origin_zones")
    for z, (lat, lng) in ZONE_COORDS.items():
        conn.execute("INSERT OR REPLACE INTO origin_zones(zone,lat,lng) VALUES(?,?,?)", (z, lat, lng))
    conn.commit()
    return {"zones": len(ZONE_COORDS)}


def load_zones(conn) -> list[tuple]:
    """Return [(folded_zone, lat, lng)] sorted by folded length DESC (longest match wins)."""
    rows = conn.execute("SELECT zone,lat,lng FROM origin_zones WHERE lat IS NOT NULL").fetchall()
    out = [(nm._fold(z), lat, lng) for (z, lat, lng) in rows]
    out.sort(key=lambda t: len(t[0]), reverse=True)
    return out


# Broad municipality names: only used if NO specific sector also matches (so
# "Naiguatá, La Guaira" -> Naiguatá, and "Los Caracas" -> Los Caracas, not Caracas).
_BROAD = {"la guaira", "guaira", "la guira", "vargas", "caracas"}


_VAGUE = ("no indicada", "no indicado", "desconocid", "sin informacion", "sin información",
          "no especific", "no se sabe", "se desconoce", "n/a")


def _is_vague(o: str) -> bool:
    f = nm._fold(o)
    return (not f) or any(v in f for v in _VAGUE)


def order_origins(origins: list[str], zones: list[tuple]) -> list[str]:
    """Most useful origin FIRST (geocodable + specific), vague ('No indicada') LAST. A deduped
    person should surface their best known location, not the vaguest report — this is the hub's job."""
    def rank(o):
        if _is_vague(o):
            return 2
        return 0 if locate([o], zones) else 1
    return sorted(origins or [], key=rank)


def locate(origins: list[str], zones: list[tuple]) -> dict | None:
    """Origin -> the most SPECIFIC matching zone (specific sector beats broad municipality;
    among equals, longest name). None if no zone token is present."""
    for o in origins or []:
        f = nm._fold(o)
        if not f:
            continue
        matches = [(zf, lat, lng) for zf, lat, lng in zones if zf in f]
        if not matches:
            continue
        specific = [m for m in matches if m[0] not in _BROAD]
        zf, lat, lng = max(specific or matches, key=lambda m: len(m[0]))
        return {"lat": lat, "lng": lng, "zone": zf}
    return None


# --- building-level geocoding (Photon / OSM, free) with a La Guaira bounding box ---
# bbox guard rejects wrong-country hits (e.g. the famous Belo Horizonte, Brazil).
LG_BBOX = (10.40, 10.74, -67.35, -66.20)   # lat_min, lat_max, lng_min, lng_max
PHOTON = "https://photon.komoot.io/api/"
_BUILDING_VALS = {"apartments", "residential", "house", "detached", "commercial",
                  "construction", "building", "yes", "tower", "hotel", "school", "hospital"}


def _ensure_cache(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS origin_geo(q TEXT PRIMARY KEY, lat REAL, lng REAL, tier TEXT, label TEXT)")


def _photon(query: str) -> dict | None:
    url = PHOTON + "?" + urllib.parse.urlencode({"q": query, "limit": 1, "lat": 10.6, "lon": -66.95})
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "directorio-sismo/1.0"}), timeout=12))
    except Exception:
        return None
    fs = d.get("features") or []
    if not fs:
        return None
    lng, lat = fs[0]["geometry"]["coordinates"]
    if not (LG_BBOX[0] <= lat <= LG_BBOX[1] and LG_BBOX[2] <= lng <= LG_BBOX[3]):
        return None   # outside La Guaira/Vargas -> reject (wrong match)
    pr = fs[0].get("properties", {})
    is_bldg = pr.get("osm_key") == "building" or (pr.get("osm_value") in _BUILDING_VALS) or (
        bool(pr.get("name")) and pr.get("osm_key") not in ("place", "boundary"))
    return {"lat": lat, "lng": lng, "tier": "building" if is_bldg else "sector",
            "label": pr.get("name") or query}


import re  # noqa: E402

_BLD = re.compile(r"\b(edif|edf|resid|residencia|residencial|torre|conjunto|bloque|quinta)\b", re.I)
# generic place/type words that don't make two names "the same building"
_GEN = {"residencia", "residencias", "residencial", "edificio", "conjunto", "torre", "playa",
        "grande", "club", "avenida", "calle", "sector", "parroquia", "urbanizacion", "urb",
        "bahia", "del", "mar", "guaira", "catia", "caraballeda", "macuto", "vargas", "venezuela",
        "caribe", "los", "las", "san", "santa", "calle", "avenida", "estado"}


def _dtoks(s: str) -> set:
    return {t for t in nm._fold(s).split() if len(t) >= 4 and t not in _GEN}


def _km(la1, lo1, la2, lo2):
    import math
    p1, p2 = math.radians(la1), math.radians(la2)
    a = math.sin(math.radians(la2 - la1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


_FOOTPRINTS = None   # centroids (lat,lng) of the affected-building footprints (inhabited ground)


def _load_footprints() -> list:
    global _FOOTPRINTS
    if _FOOTPRINTS is not None:
        return _FOOTPRINTS
    import os
    path = Path(os.environ.get("DATA_DIR", ROOT / "out")) / "edificios_afectados.geojson"
    pts = []
    try:
        d = json.loads(path.read_text())
        for f in d.get("features", []):
            xs, ys = [], []
            def walk(o):
                if isinstance(o, list) and o and isinstance(o[0], (int, float)):
                    xs.append(o[0]); ys.append(o[1])
                elif isinstance(o, list):
                    for x in o:
                        walk(x)
            walk((f.get("geometry") or {}).get("coordinates") or [])
            if xs:
                pts.append((sum(ys) / len(ys), sum(xs) / len(xs)))
    except Exception:
        pass
    _FOOTPRINTS = pts
    return pts


def snap_to_building(lat: float, lng: float, max_km: float = 2.0) -> tuple | None:
    """Nearest real building footprint within max_km — so approximate points land on
    inhabited ground, never in the sea. None if no footprint is near (outside coverage)."""
    pts = _load_footprints()
    if not pts:
        return None
    best, bd = None, 1e9
    for (la, ln) in pts:
        if abs(la - lat) > 0.03 or abs(ln - lng) > 0.03:   # cheap bbox prefilter (~3km)
            continue
        d = _km(lat, lng, la, ln)
        if d < bd:
            bd, best = d, (la, ln)
    return best if bd <= max_km else None


def geocode_origin(conn, origin: str, zones: list[tuple]) -> dict | None:
    """cache -> (if origin NAMES a building) Photon, accepted only when the result shares a
    distinctive token -> else sector gazetteer. Sector-only origins never trust a fuzzy building."""
    _ensure_cache(conn)
    key = nm._fold(origin)[:160]
    if not key:
        return None
    row = conn.execute("SELECT lat,lng,tier,label FROM origin_geo WHERE q=?", (key,)).fetchone()
    if row:
        return None if row[0] is None else {"lat": row[0], "lng": row[1], "tier": row[2], "label": row[3]}
    res = None
    anchor = locate([origin], zones)              # the sector the origin claims (if any)
    if _BLD.search(origin):                       # origin explicitly names a building
        seg = origin.split(",")[0].strip()
        ph = _photon(origin) or (_photon(seg + ", La Guaira") if seg else None)
        if ph and ph["tier"] == "building" and (_dtoks(origin) & _dtoks(ph["label"])):
            # accept ONLY if it sits near the claimed sector (kills wrong-instance matches:
            # "Las Palmas" in Caracas vs Macuto). No sector claimed -> require the core strip.
            near = _km(ph["lat"], ph["lng"], anchor["lat"], anchor["lng"]) <= 4.0 if anchor \
                else (10.58 <= ph["lat"] <= 10.63 and -67.10 <= ph["lng"] <= -66.66)
            if near:
                res = ph
    if not res and anchor:                        # safe coarse fallback: sector centroid
        res = {"lat": anchor["lat"], "lng": anchor["lng"], "tier": "sector", "label": anchor["zone"]}
    if res and res["tier"] in ("sector", "zone"):  # clamp approximate points onto real ground
        sn = snap_to_building(res["lat"], res["lng"])
        if sn:
            res["lat"], res["lng"] = sn[0], sn[1]
    conn.execute("INSERT OR REPLACE INTO origin_geo(q,lat,lng,tier,label) VALUES(?,?,?,?,?)",
                 (key, res["lat"] if res else None, res["lng"] if res else None,
                  res["tier"] if res else None, res["label"] if res else None))
    conn.commit()
    return res


def attach(conn, people: list[dict], zones: list[tuple] | None = None, max_live: int = 8) -> list[dict]:
    """Add lat/lng/geo_tier to people. Cache-first; up to max_live live Photon lookups per
    call (rest fall back to the in-memory zone centroid), so requests stay bounded."""
    zones = zones if zones is not None else load_zones(conn)
    live = 0
    for p in people:
        origs = order_origins(p.get("origins") or p.get("org") or [], zones)  # best place first
        if "origins" in p:
            p["origins"] = origs   # so the card/answer shows the real place, not "No indicada"
        o = origs[0] if origs else ""
        loc = None
        if o:
            cached = conn.execute("SELECT lat,lng,tier FROM origin_geo WHERE q=?", (nm._fold(o)[:160],)).fetchone() \
                if o else None
            if cached and cached[0] is not None:
                loc = {"lat": cached[0], "lng": cached[1], "tier": cached[2]}
            elif live < max_live:
                loc = geocode_origin(conn, o, zones); live += 1
        if not loc:                    # always-available coarse fallback (no network)
            z = locate(origs, zones)
            if z:
                sn = snap_to_building(z["lat"], z["lng"])
                loc = {"lat": sn[0] if sn else z["lat"], "lng": sn[1] if sn else z["lng"], "tier": "zone"}
        if loc:
            p["lat"], p["lng"], p["geo_tier"] = loc["lat"], loc["lng"], loc.get("tier", "zone")
    return people


if __name__ == "__main__":
    c = db.connect()
    print(ensure_zones(c))
