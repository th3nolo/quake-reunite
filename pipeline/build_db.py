"""Generate the self-contained buscador.html from the LIVE SQLite DB.

The legacy pipeline/build.py builds the search page from text+photo files only.
This builds the SAME page (same template/UX) from the resolved DB (store_db), so it
reflects the FULL current index — VR API + photos + web + federation, deduped +
household-linked. The maintainer calls this each cycle so the served page stays fresh.

  python pipeline/build_db.py        # -> out/buscador.html from out/directorio.db
"""
from __future__ import annotations

import json, sqlite3, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
import os  # noqa: E402
import store_db as db   # noqa: E402
import build as legacy  # noqa: E402  (reuse HTML_TEMPLATE — identical UX)

# Write next to the DB (DATA_DIR), so the API serves the same file the maintainer writes.
OUT = Path(os.environ.get("DATA_DIR", ROOT / "out"))

_VAGUE = ("no indicada", "no indicado", "desconocid", "sin informacion", "no especific")


def _org_order(origins):
    """Surface the real place first; push 'No indicada' / vague reports to the end."""
    return sorted(origins or [], key=lambda o: 1 if (not o or any(v in o.lower() for v in _VAGUE)) else 0)


def export() -> dict:
    conn = sqlite3.connect(db.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    # appearances grouped per person (slim: drop bulky obs/row_raw)
    ap = defaultdict(list)
    for r in conn.execute("SELECT person_id,hospital,source,status,ci,age,origin,date FROM records"):
        ap[r["person_id"]].append({"h": r["hospital"] or "", "s": r["source"] or "",
                                   "st": r["status"] or "", "ci": r["ci"] or "",
                                   "age": str(r["age"] or ""), "org": r["origin"] or "",
                                   "date": r["date"] or ""})

    people = []
    for p in conn.execute("SELECT * FROM persons ORDER BY n_records DESC"):
        d = db.person_to_dict(p)
        hosp = d["hospitals"]
        people.append({
            "id": d["person_id"], "n": d["display_name"], "k": p["name_key"] or d["display_name"],
            "ci": d["all_ci"], "age": (d["ages"][0] if d["ages"] else ""), "sex": d["sex"],
            "org": _org_order(d["origins"]), "hosp": hosp, "st": d["statuses"],
            "dec": d["deceased"], "multi": len(hosp) > 1, "cic": d["ci_conflict"],
            "ps": [], "ap": ap.get(p["person_id"], []),
        })
    conn.close()

    n_people = len(people)
    n_records = sum(len(v) for v in ap.values())
    n_multi = sum(1 for p in people if p["multi"])
    n_hosp = len({h for p in people for h in p["hosp"]})

    page = legacy.HTML_TEMPLATE
    page = page.replace("/*DATA*/", "const PEOPLE = " + json.dumps(people, ensure_ascii=False) + ";")
    page = (page.replace("{{N_PEOPLE}}", str(n_people)).replace("{{N_RECORDS}}", str(n_records))
                .replace("{{N_MULTI}}", str(n_multi)).replace("{{N_HOSP}}", str(n_hosp)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "buscador.html").write_text(page, encoding="utf-8")
    return {"people": n_people, "records": n_records, "multi": n_multi, "bytes": len(page)}


if __name__ == "__main__":
    print(export())
