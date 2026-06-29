"""Venezuela Reporta — open API connector + cleanup (NOT scraping).

GET https://venezuelareporta.org/api/v1/personas  -> consolidated person registry
as JSON. Open/free, read-only, attribution required, no contact data, 120 req/min
+ 60s cache, supports ?since= for incrementals.

Cleanup (the `nombre` field is messy free-text):
  * deterministic: pull cedulas embedded in the name, strip them, drop junk.
  * Gemma: ONLY for multi-person entries ("X CI 1 Y Z CI 2") -> split into people.
status buscando -> por_localizar; a_salvo/encontrado -> localizado.
"""
from __future__ import annotations

import csv, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz  # noqa: E402
from clients import gemma_jsonl  # noqa: E402

API = "https://venezuelareporta.org/api/v1/personas"
ATTR = "Venezuela Reporta — venezuelareporta.org"
UA = "directorio/1.0 (humanitarian reunification)"
STATUS_MAP = {"buscando": "por_localizar", "a_salvo": "localizado", "encontrado": "localizado"}
GENDER = {"femenino": "F", "masculino": "M", "f": "F", "m": "M"}
REC_COLS = ["source", "source_type", "hospital", "apellidos", "nombres", "full_name", "name_key",
            "ci", "age", "age_unit", "sex", "origin", "status", "obs", "date", "row_raw"]

CI_NUM = re.compile(r'(?<![\d.])(?:[VvEe][-\s]?)?(\d{1,2}[.\s]?\d{3}[.\s]?\d{3}|\d{6,9})(?![\d.])')
CI_LABEL = re.compile(r'\b(c\.?\s*i\.?|c[eé]dula|ci)\b[:\-\s.]*', re.I)
CONNECT = re.compile(r'\b(y|e)\b|&|/', re.I)


def find_cedulas(text: str) -> list[str]:
    out, seen = [], set()
    for m in CI_NUM.finditer(text or ""):
        d = re.sub(r'\D', '', m.group(0))
        if 6 <= len(d) <= 9 and d not in seen:
            seen.add(d); out.append(d)
    return out


def strip_cedulas(text: str) -> str:
    t = CI_NUM.sub(' ', text or '')
    t = CI_LABEL.sub(' ', t)
    t = re.sub(r'[^\w\sÁÉÍÓÚÜÑáéíóúüñ]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def is_junk(name_clean: str) -> bool:
    toks = [t for t in name_clean.split() if len(t) > 1 and any(c.isalpha() for c in t)]
    return len(toks) < 1


def is_multi(name: str, cis: list[str]) -> bool:
    if len(cis) >= 2:
        return True
    nclean = strip_cedulas(name)
    return len(nclean.split()) >= 4 and bool(CONNECT.search(nclean))


def _fetch(status: str, offset: int, since: str | None, limit: int = 100) -> dict:
    params = {"status": status, "limit": limit, "offset": offset}
    if since:
        params["since"] = since
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _record(name: str, ci: str, edad, genero: str, ciudad: str, zona: str, status: str, vid: str) -> dict:
    name = name.strip()
    origin = (nz.clean_display(ciudad) + (" / " + nz.clean_display(zona)
              if zona and zona != ciudad else "")).strip(" /")
    return {
        "source": "venezuelareporta", "source_type": "api", "hospital": "",
        "apellidos": "", "nombres": name.upper(), "full_name": name.upper(),
        "name_key": nz.name_key("", name), "ci": nz.normalize_ci(str(ci or "")),
        "age": str(edad or "").strip(), "age_unit": "years" if edad else "",
        "sex": GENDER.get(str(genero or "").lower(), ""),
        "origin": origin, "status": STATUS_MAP.get(status, status),
        "obs": f"{ATTR} | id={vid}", "date": "", "row_raw": (zona or "")[:120],
    }


def _gemma_split(batch: list[dict]) -> dict[int, list[dict]]:
    lines = "\n".join(f"[{i}] {b['nombre']}" for i, b in enumerate(batch))
    prompt = ("You clean messy missing-person entries from a Venezuelan registry. Each line has an "
              "index and a raw name field that may contain ONE OR MORE people, with cédulas (IDs) "
              "embedded. For each index return one JSON object per line (JSONL):\n"
              '{"i": <index>, "personas": [{"nombre":"<clean full name>","ci":"<digits or ''>"}]}\n'
              "Split distinct people; move cédulas into ci (digits only); junk/test/non-name -> "
              '"personas": []. Never invent.\nINPUT:\n' + lines)
    out: dict[int, list[dict]] = {}
    for row in gemma_jsonl(prompt, max_tokens=8000):
        if isinstance(row, dict) and "i" in row and isinstance(row.get("personas"), list):
            out[int(row["i"])] = row["personas"]
    return out


def _id_from_obs(obs: str) -> str:
    m = re.search(r'id=([^\s|]+)', obs or "")
    return m.group(1) if m else ""


def pull(statuses=("buscando",), since: str | None = None, max_per_status: int | None = None,
         clean: bool = True, merge: bool = False, out_csv: str | None = None, delay: float = 0.55) -> int:
    out_csv = out_csv or str(ROOT / "ingest" / "out" / "vr_missing.csv")
    raw: list[dict] = []
    for status in statuses:
        offset, total = 0, None
        while True:
            try:
                d = _fetch(status, offset, since)
            except Exception as e:
                print(f"\n  ! {status} offset={offset}: {e}"); break
            total = d.get("total"); people = d.get("personas", [])
            if not people:
                break
            for p in people:
                p["_status"] = status
            raw.extend(people)
            offset += len(people)
            print(f"  pull {status}: {offset}/{total}", end="\r", flush=True)
            if (max_per_status and offset >= max_per_status) or (total and offset >= total):
                break
            time.sleep(delay)
        print()

    rows: list[dict] = []
    gemma_q: list[dict] = []
    for p in raw:
        name = nz.clean_display(p.get("nombre", "")); api_ci = nz.normalize_ci(str(p.get("cedula") or ""))
        cis = find_cedulas(name); name_clean = strip_cedulas(name)
        if is_junk(name_clean) and not api_ci and not cis:
            continue
        if clean and is_multi(name, cis):
            gemma_q.append(p); continue
        ci = api_ci or (cis[0] if cis else "")
        rows.append(_record(name_clean, ci, p.get("edad"), p.get("genero"),
                            p.get("ciudad", ""), p.get("zona", ""), p["_status"], p.get("id", "")))

    if gemma_q:
        print(f"  Gemma cleanup on {len(gemma_q)} multi/messy entries...")
        for s in range(0, len(gemma_q), 40):
            batch = gemma_q[s:s + 40]
            try:
                res = _gemma_split(batch)
            except Exception as e:
                res = {}; print(f"  ! gemma batch {s}: {e}")
            for i, p in enumerate(batch):
                personas = res.get(i)
                if not personas:  # fallback: deterministic single
                    nc = strip_cedulas(p.get("nombre", "")); fc = find_cedulas(p.get("nombre", ""))
                    if not is_junk(nc):
                        rows.append(_record(nc, nz.normalize_ci(str(p.get("cedula") or "")) or (fc[0] if fc else ""),
                                    p.get("edad"), p.get("genero"), p.get("ciudad", ""), p.get("zona", ""),
                                    p["_status"], p.get("id", "")))
                    continue
                solo = len(personas) == 1
                for k, per in enumerate(personas):
                    nm = nz.clean_display(per.get("nombre", ""))
                    if not nm or is_junk(strip_cedulas(nm)):
                        continue
                    vid = p.get("id", "") if k == 0 else f"{p.get('id','')}#{k}"
                    rows.append(_record(nm, per.get("ci", ""), p.get("edad") if solo else "",
                                p.get("genero") if solo else "", p.get("ciudad", ""), p.get("zona", ""),
                                p["_status"], vid))

    # write (merge by VR id on incremental, else overwrite)
    final = rows
    pth = Path(out_csv)
    if merge and pth.exists():
        by_id = {}
        for r in csv.DictReader(open(out_csv, encoding="utf-8")):
            by_id[_id_from_obs(r.get("obs", "")) or id(r)] = r
        for r in rows:
            by_id[_id_from_obs(r["obs"]) or id(r)] = r
        final = list(by_id.values())
    pth.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REC_COLS, extrasaction="ignore"); w.writeheader(); w.writerows(final)
    print(f"  wrote {len(rows)} new ({len(final)} total) -> {out_csv}  [pulled {len(raw)}, gemma {len(gemma_q)}]")
    return len(rows)


def search(q: str, limit: int = 25) -> list[dict]:
    """Live single-query search of the VR API (for demand-driven federation).
    Deterministic cleanup only (no Gemma) so it's fast enough for a /ask request."""
    url = f"{API}?{urllib.parse.urlencode({'q': q, 'limit': limit})}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=20))
    except Exception:
        return []
    out = []
    for p in d.get("personas", []):
        name = nz.clean_display(p.get("nombre", "")); api_ci = nz.normalize_ci(str(p.get("cedula") or ""))
        cis = find_cedulas(name); name_clean = strip_cedulas(name)
        if is_junk(name_clean) and not api_ci and not cis:
            continue
        ci = api_ci or (cis[0] if cis else "")
        out.append(_record(name_clean, ci, p.get("edad"), p.get("genero"),
                           p.get("ciudad", ""), p.get("zona", ""), p.get("_status", "buscando"), p.get("id", "")))
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    statuses = tuple((a[a.index("--status") + 1].split(",")) if "--status" in a else ("buscando",))
    maxn = int(a[a.index("--max") + 1]) if "--max" in a else None
    since = a[a.index("--since") + 1] if "--since" in a else None
    merge = "--merge" in a
    pull(statuses=statuses, since=since, max_per_status=maxn, clean=True, merge=merge)
