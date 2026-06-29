"""Fuse every person source -> resolve.cluster -> central deduped outputs.

Person sources (configurable): consolidated PDF/DOCX (pipeline.parse_text),
OCR+Gemma photos (ingest/out/photos_records.csv), web (web_persons.csv),
video (video_persons.csv). Photos go through the OCR+Gemma path, so the legacy
parse_photos is intentionally NOT used here (OCR captures more cedulas).

Outputs (to out/ unless DATA_DIR set):
  central_people.json   central_records.csv   central_centers.csv   central_review.md
"""
from __future__ import annotations

import csv, json, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz  # noqa: E402
from resolve import cluster  # noqa: E402
from build import add_possible_same, assign_record_ids, assign_person_ids  # noqa: E402

OUT = Path(os.environ.get("DATA_DIR", ROOT / "out"))
REC_COLS = ["record_id", "person_id", "source", "source_type", "hospital", "apellidos",
            "nombres", "full_name", "name_key", "ci", "age", "age_unit", "sex", "origin",
            "status", "obs", "date", "row_raw"]


def _load_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    for r in rows:
        for c in REC_COLS:
            r.setdefault(c, "")
    return rows


def gather_persons(include_pdf: bool = True) -> list[dict]:
    recs: list[dict] = []
    ing = ROOT / "ingest" / "out"
    if include_pdf:
        try:
            from parse_text import parse_all_text
            recs += parse_all_text()
        except Exception as e:
            print(f"[warn] parse_text skipped: {e}")
    for name in ("photos_records.csv", "web_persons.csv", "video_persons.csv", "vr_missing.csv"):
        recs += _load_csv(ing / name)
    # ensure required keys
    for r in recs:
        for c in REC_COLS:
            r.setdefault(c, "")
    return recs


def gather_centers() -> list[dict]:
    path = ROOT / "ingest" / "out" / "web_centers.csv"
    rows = list(csv.DictReader(open(path, encoding="utf-8"))) if path.exists() else []
    seen, out = {}, []
    for r in rows:
        key = nz.name_key(r.get("name", ""), "") + "|" + nz.fold(r.get("municipality", ""))
        if key in seen:
            c = seen[key]
            for f in ("address", "hours", "phone", "needs", "operator"):
                if r.get(f) and not c.get(f):
                    c[f] = r[f]
            continue
        seen[key] = dict(r)
        out.append(seen[key])
    return out


def run(include_pdf: bool = True) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    records = gather_persons(include_pdf)
    assign_record_ids(records)
    people = cluster(records)
    assign_person_ids(people, records)
    add_possible_same(people)
    centers = gather_centers()

    (OUT / "central_people.json").write_text(json.dumps(people, ensure_ascii=False), encoding="utf-8")
    with (OUT / "central_records.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REC_COLS, extrasaction="ignore"); w.writeheader(); w.writerows(records)
    if centers:
        with (OUT / "central_centers.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(centers[0].keys())); w.writeheader(); w.writerows(centers)

    name_only = sum(1 for p in people if p["only_name_merge"] and p["n_records"] > 1)
    ci_conf = sum(1 for p in people if p["ci_conflict"])
    deceased = sum(1 for p in people if p["deceased"])
    review = [p for p in people if p.get("possible_same")]
    (OUT / "central_review.md").write_text(
        f"# Central review\n\n- records: {len(records)}\n- people: {len(people)}\n"
        f"- with cedula: {sum(1 for p in people if p['ci'])}\n"
        f"- name-only merges: {name_only}\n- ci-conflicts: {ci_conf}\n- deceased: {deceased}\n"
        f"- possible-same pairs to review: {len(review)}\n- centers: {len(centers)}\n", encoding="utf-8")
    summary = {"records": len(records), "people": len(people), "centers": len(centers),
               "with_cedula": sum(1 for p in people if p["ci"]), "name_only": name_only,
               "review_pairs": len(review)}
    print("central:", summary)
    return summary


if __name__ == "__main__":
    run(include_pdf="--no-pdf" not in sys.argv)
