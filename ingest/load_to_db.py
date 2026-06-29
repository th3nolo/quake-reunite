"""Stream all person sources into the SQLite backend with incremental dedup.

Reads each CSV row-by-row (flat memory) and calls store_db.add_record, which
resolves it against the DB on insert. Centers from web_centers.csv. Commits in
batches so a crash loses at most one batch, not the whole run.

  python ingest/load_to_db.py            # rebuild from scratch
  python ingest/load_to_db.py --no-reset # append into existing DB
  python ingest/load_to_db.py --no-pdf   # skip the consolidated PDF/DOCX
"""
from __future__ import annotations

import csv, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
import store_db as db  # noqa: E402

ING_OUT = ROOT / "ingest" / "out"
PERSON_CSVS = ["photos_records.csv", "web_persons.csv", "vr_missing.csv", "video_persons.csv"]
BATCH = 1000


def _stream_csv(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            yield row


def rebuild(reset: bool = True, include_pdf: bool = True) -> dict:
    if reset and Path(db.DB_PATH).exists():
        for ext in ("", "-wal", "-shm"):
            Path(db.DB_PATH + ext).unlink(missing_ok=True)
    conn = db.connect()
    n, t0 = 0, time.time()

    def feed(it, label):
        nonlocal n
        c = 0
        for rec in it:
            db.add_record(conn, rec)
            n += 1; c += 1
            if n % BATCH == 0:
                conn.commit()
                print(f"  {n} records ({label})...", end="\r", flush=True)
        conn.commit()
        print(f"  {label}: +{c} (total {n})            ")

    if include_pdf:
        try:
            from parse_text import parse_all_text
            feed(iter(parse_all_text()), "pdf/docx")
        except Exception as e:
            print(f"  [warn] parse_text skipped: {e}")
    for name in PERSON_CSVS:
        feed(_stream_csv(ING_OUT / name), name)

    # centers
    cpath = ING_OUT / "web_centers.csv"
    if cpath.exists():
        cc = 0
        for c in _stream_csv(cpath):
            db.add_center(conn, c); cc += 1
        conn.commit()
        print(f"  centers: {cc}")

    import households
    hh = households.rebuild(conn)
    print("  households:", hh)

    import centers_enrich
    ce = centers_enrich.enrich(conn)   # needs taxonomy + type + geocode (new centers only)
    print("  centers enriched:", ce)

    import centers_dedup
    cd = centers_dedup.reconcile(conn)  # cross-source dup merge (soft, after enrich tags rows)
    print("  centers deduped:", cd)

    s = db.stats(conn)
    s.update(hh)
    s["seconds"] = round(time.time() - t0, 1)
    conn.close()
    print("DB:", s)
    return s


if __name__ == "__main__":
    rebuild(reset="--no-reset" not in sys.argv, include_pdf="--no-pdf" not in sys.argv)
