"""Demand-driven federated cross-check — ETHICAL by construction.

Triggered ONLY by a real user's /ask about a specific person. For that one
identity (cédula or full name) we query external sources, ingest what they
return, and answer. The index grows from genuine demand — never enumeration.

Safeguards:
  * identity-scoped: we only ever query the exact person asked about.
  * TTL cache: a (source, identity) checked recently is NOT re-hit (store_db.checked).
  * per-source rate limit: even with many users, no source gets flooded.

Source kinds:
  * "api"       -> direct GET of an open/sanctioned API (Venezuela Reporta live).
  * "firecrawl" -> scrape the platform's PUBLIC search for this person, Gemma
                   parses the result. This is the closed apps' intended per-person
                   "confirm" use — not bulk scraping.
"""
from __future__ import annotations

import json, os, subprocess, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz          # noqa: E402
import store_db as db           # noqa: E402
import extract_vr_api as vr     # noqa: E402
from clients import gemma_jsonl  # noqa: E402
from ratelimit import TokenBucket  # noqa: E402

# Re-fetch throttle: how long before we'll ask the SAME source about the SAME
# person again. The person DATA is always ingested+deduped+persisted on the first
# hit regardless of this — this only stops redundant external calls. Short = fresher
# (catches missing->found sooner), longer = gentler on the source. Not a data cache.
REFETCH_THROTTLE_MIN = int(os.environ.get("REFETCH_THROTTLE_MIN", "30"))
_BUCKETS: dict[str, TokenBucket] = {}

# Closed sources start disabled until their public search URL is verified
# (a firecrawl discovery pass fills `search_url`). vr_live is open + sanctioned.
# Endpoints discovered by the firecrawl agent fan-out (verified to return per-person results).
# "http" = direct GET of the site's own public search API/page (Gemma parses the response).
SOURCES = [
    {"id": "vr_live", "kind": "api", "enabled": True,
     "note": "Venezuela Reporta open API (live, sanctioned)"},
    {"id": "encuentralos", "kind": "http", "enabled": True,
     "search_url": "https://encuentralos.tecnosoft.dev/api/personas?q={q}",
     "note": "found Gabriela here; indexes by name (no cédula)"},
    {"id": "sosvenezuela2026", "kind": "http", "enabled": True,
     "search_url": "https://sosvenezuela2026.com/buscar?q={q}"},
    {"id": "radarvzla", "kind": "http", "enabled": True,
     "search_url": "https://radarvzla.com/api/buscar?modo=todo&q={q}"},
    # JS-only search (need firecrawl interact) — left disabled until that path is wired:
    {"id": "hospitalesve", "kind": "firecrawl", "enabled": False,
     "search_url": "https://hospitalesenvenezuela.com/?q={q}", "note": "interact-only"},
    {"id": "venezuelatebusca", "kind": "firecrawl", "enabled": False,
     "search_url": "https://venezuelatebusca.com/?q={q}", "note": "interact-only"},
    {"id": "desaparecidos", "kind": "firecrawl", "enabled": False,
     "search_url": "https://desaparecidosterremotovenezuela.com/?q={q}", "note": "interact-only"},
]

FC_PROMPT = ("From this PUBLIC search-results page of '{site}' for query '{q}', extract ONLY people "
             "that match the query. JSONL, one per line: "
             '{{"nombre":"","ci":"","estado":"","hospital":"","lugar":""}}. '
             "If none match, output nothing. Never invent.\nPAGE:\n")


def _bucket(sid: str) -> TokenBucket:
    return _BUCKETS.setdefault(sid, TokenBucket(req_per_min=30, tok_per_min=10_000))


def _identity(ci: str, name: str) -> str:
    return "ci:" + ci if ci else "nm:" + nz.name_key(name, "")


def cedula_variants(ci: str) -> list[str]:
    """External sources store cédulas differently: '12345678', '12.345.678',
    'V-12.345.678', 'V12345678'. Try them all when querying (we always store digits)."""
    d = "".join(c for c in (ci or "") if c.isdigit())
    if not d:
        return []
    rev = d[::-1]
    dotted = ".".join(rev[i:i + 3] for i in range(0, len(rev), 3))[::-1]
    return [d, dotted, "V-" + dotted, "V" + d, "V-" + d]


def _query_variants(ci: str, name: str) -> list[str]:
    qs = cedula_variants(ci)
    if name and len(nz.name_key(name, "").split()) >= 2:
        try:
            import names as _n          # misspellings (dropped/added H, s/z), apellido swap
            qs += _n.name_variants(name, max_n=5)
        except Exception:
            qs.append(name)
    seen, out = set(), []
    for q in qs:
        if q and q not in seen:
            seen.add(q); out.append(q)
    return out


def _norm_status(s: str) -> str:
    s = (s or "").lower()
    if "desaparecid" in s or "buscando" in s or "localizar" in s:
        return "por_localizar"
    if "localizad" in s or "encontrad" in s or "salvo" in s:
        return "localizado"
    if "fallecid" in s or "muert" in s:
        return "fallecido"
    if "ingres" in s or "hospital" in s:
        return "ingresado"
    return s


def _map_rows(src: dict, rows: list) -> list[dict]:
    out = []
    for r in rows:
        ap = nz.clean_display(r.get("nombre", ""))
        if not ap:
            continue
        out.append({"source": src["id"], "source_type": "federated", "hospital": nz.clean_display(r.get("hospital", "")),
                    "apellidos": "", "nombres": ap.upper(), "full_name": ap.upper(),
                    "name_key": nz.name_key("", ap), "ci": nz.normalize_ci(str(r.get("ci", ""))),
                    "age": str(r.get("edad", "") or "").strip(), "age_unit": "", "sex": "",
                    "origin": nz.clean_display(str(r.get("lugar", ""))),
                    "status": _norm_status(r.get("estado", "")) or "por_localizar",
                    "obs": f"{src['id']} (federated)", "date": "", "row_raw": ""})
    return out


def _gemma_parse(src: dict, q: str, text: str) -> list[dict]:
    if not text.strip():
        return []
    return _map_rows(src, gemma_jsonl(FC_PROMPT.format(site=src["id"], q=q) + text[:25000], max_tokens=2000))


def _http_get_records(src: dict, q: str) -> list[dict]:
    """Direct GET of a source's own public search API/page; Gemma parses the response."""
    url = src["search_url"].format(q=urllib.parse.quote(q))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "directorio/1.0", "Accept": "application/json"})
        text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception:
        return []
    return _gemma_parse(src, q, text)


def _fc_records(src: dict, q: str) -> list[dict]:
    url = src["search_url"].format(q=urllib.parse.quote(q))
    try:
        md = subprocess.run(["firecrawl", "scrape", url, "-o", "/dev/stdout"],
                            capture_output=True, text=True, timeout=75).stdout
    except Exception:
        return []
    return _gemma_parse(src, q, md)


def _probe(src: dict, variants: list) -> tuple:
    """Network-only: try each query variant on ONE source until one hits. No DB writes."""
    recs, used, k = [], "", src["kind"]
    for q in variants:
        _bucket(src["id"]).acquire(1)
        try:
            r = vr.search(q) if k == "api" else _http_get_records(src, q) if k == "http" else _fc_records(src, q)
        except Exception:
            r = []
        if r:
            recs, used = r, q; break        # first variant that hits wins
    return src, recs, used


def check(conn, ci: str = "", name: str = "", sources: list | None = None) -> dict:
    """Cross-check ONE identity against external sources; ingest; return summary.
    Sources are probed CONCURRENTLY (network), then results written serially (one SQLite
    writer). Variants are capped so a not-found query can't fan out forever."""
    ci = "".join(c for c in (ci or "") if c.isdigit())
    if not ci and len(nz.name_key(name, "").split()) < 2:
        return {"skipped": "need a cédula or a full name (no broad enumeration)"}
    ident = _identity(ci, name)
    variants = _query_variants(ci, name)[:3]          # cap: bounds the not-found cost
    enabled = sources or [s for s in SOURCES if s["enabled"]]
    todo = [s for s in enabled if not db.recently_checked(conn, s["id"], ident, REFETCH_THROTTLE_MIN)]
    log = [f"{s['id']}:throttled" for s in enabled if s not in todo]
    fetched = 0
    if todo:
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as ex:   # probe sources in parallel
            results = list(ex.map(lambda s: _probe(s, variants), todo))
        for src, recs, used in results:               # DB writes serialized after the network fan-out
            for r in recs:
                db.add_record(conn, r)                # idempotent -> only genuinely new rows persist
            db.mark_checked(conn, src["id"], ident)
            log.append(f"{src['id']}:{len(recs)}" + (f" via '{used}'" if used else ""))
            fetched += len(recs)
        conn.commit()
    return {"identity": ident, "sources": log, "fetched": fetched}
