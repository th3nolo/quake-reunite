"""Web page -> records, via Gemma structuring of page markdown.

Reality (verified on the cached scrapes): the big person registries
(hospitalesenvenezuela, desaparecidos, venezuelatebusca...) are SEARCH-ONLY /
JS apps and by privacy design do NOT bulk-publish people. So this connector
yields mostly AID CENTERS (acopio/refugio) + persons only from pages that
actually render lists server-side (e.g. venezuelareporta.org). UI-only shells
correctly return nothing.

Input is page markdown (from the .firecrawl cache or a fresh fetch).
"""
from __future__ import annotations

import csv
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz  # noqa: E402
from clients import gemma_jsonl  # noqa: E402

WEB_PROMPT = """This is the markdown of a Venezuelan 2026-earthquake relief web page. Extract ONLY entities that actually appear as DATA on the page. Return JSONL, one JSON object per line. No prose, no fences.

If the page is just a search box / landing UI with NO listed people or centers, return NOTHING (empty output).

For a listed PERSON (hospitalized/missing/found):
{"kind":"person","apellidos":"","nombres":"","ci":"<digits or '' >","age":"","sex":"M/F/''","status":"ingresado/alta/fallecido/por_localizar/''","hospital":"","origin":"","raw":"<source line>"}

For a listed AID CENTER (centro de acopio / refugio / albergue):
{"kind":"center","name":"","ctype":"acopio/refugio/hub","address":"","municipality":"","hours":"","needs":"<comma list>","phone":"","operator":"gov/ngo/church/citizen/''","raw":"<source line>"}

Rules: transcribe faithfully, never invent. ci = digits only. SKIP template/sample rows
(names or addresses containing "ejemplo", "example", "demo", "plantilla")."""

# Pages sometimes ship a sample/template row (e.g. "Acopio de Ejemplo"). Drop those so
# placeholders never enter the index, even if the model transcribes them.
_PLACEHOLDER = ("ejemplo", "example", "demo", "plantilla", "lorem", "placeholder", "tu nombre", "nombre del centro")


def _is_placeholder(*fields: str) -> bool:
    t = " ".join((f or "").lower() for f in fields)
    return any(p in t for p in _PLACEHOLDER)


def fetch_markdown(url: str, timeout: int = 40) -> str:
    """Best-effort fresh fetch. For JS-heavy pages, prefer running firecrawl into
    the .firecrawl cache and passing the file instead."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 directorio/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def extract_web(markdown: str, source: str, url: str = "") -> dict:
    if not markdown.strip():
        return {"persons": [], "centers": []}
    rows = gemma_jsonl(WEB_PROMPT + "\n\nPAGE:\n" + markdown[:60000], max_tokens=16000)
    persons, centers = [], []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("kind") == "person":
            ap = nz.clean_display(r.get("apellidos", "")); no = nz.clean_display(r.get("nombres", ""))
            if not (ap or no):
                continue
            persons.append({
                "source": source, "source_type": "web_gemma",
                "hospital": nz.canonical_hospital(str(r.get("hospital", ""))) or nz.clean_display(str(r.get("hospital", ""))),
                "apellidos": ap.upper(), "nombres": no.upper(),
                "full_name": (ap + " " + no).strip().upper(),
                "name_key": nz.name_key(ap, no),
                "ci": nz.normalize_ci(str(r.get("ci", ""))),
                "age": str(r.get("age", "")).strip(), "age_unit": "",
                "sex": nz.parse_sex(str(r.get("sex", ""))),
                "origin": nz.clean_display(str(r.get("origin", ""))),
                "status": str(r.get("status", "")).strip().lower(),
                "obs": "", "date": "", "row_raw": nz.clean_display(str(r.get("raw", ""))),
            })
        elif r.get("kind") == "center":
            name = nz.clean_display(r.get("name", ""))
            if not name or _is_placeholder(name, str(r.get("address", ""))):
                continue
            centers.append({
                "source": source, "url": url, "name": name,
                "ctype": str(r.get("ctype", "")).strip().lower(),
                "address": nz.clean_display(str(r.get("address", ""))),
                "municipality": nz.clean_display(str(r.get("municipality", ""))),
                "hours": nz.clean_display(str(r.get("hours", ""))),
                "needs": nz.clean_display(str(r.get("needs", ""))),
                "phone": nz.clean_display(str(r.get("phone", ""))),
                "operator": str(r.get("operator", "")).strip().lower(),
            })
    return {"persons": persons, "centers": centers}


if __name__ == "__main__":
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/home/th3nolo/.firecrawl/ve-resources")
    all_p, all_c = [], []
    for md_file in sorted(cache.glob("*.md")):
        md = md_file.read_text(encoding="utf-8", errors="replace")
        res = extract_web(md, source=md_file.stem)
        np_, nc_ = len(res["persons"]), len(res["centers"])
        if np_ or nc_:
            print(f"  {md_file.name}: {np_} persons, {nc_} centers")
        all_p += res["persons"]; all_c += res["centers"]
    out = ROOT / "ingest" / "out"
    out.mkdir(parents=True, exist_ok=True)
    if all_p:
        with (out / "web_persons.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_p[0].keys())); w.writeheader(); w.writerows(all_p)
    if all_c:
        with (out / "web_centers.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_c[0].keys())); w.writeheader(); w.writerows(all_c)
    print(f"\nTOTAL web: {len(all_p)} persons, {len(all_c)} centers -> ingest/out/web_*.csv")
