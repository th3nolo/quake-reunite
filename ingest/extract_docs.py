"""Document -> records, two stages:
   1) Mistral OCR : photo/PDF -> faithful markdown (captures cedulas, ages, origins)
   2) Gemma-4     : markdown   -> structured rows in the directorio records schema

Rows are normalized through the existing pipeline/normalize.py so they drop
straight into resolve.cluster(). Names from handwriting are unreliable -> those
rows (esp. no-CI) are exactly what the resolver must send to review, never auto-merge.
"""
from __future__ import annotations

import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz  # noqa: E402
from clients import ocr_document, gemma_jsonl  # noqa: E402

STRUCT_PROMPT = """You are given the OCR text (markdown) of a Venezuelan hospital admission / patient list after the 2026 earthquake. Hospital context: {hospital}.

Return JSONL: ONE compact JSON object per line, one per PERSON, in order. No array brackets, no markdown fences, no commentary. Each object on its own line. Fields:
- apellidos: surnames (best effort; if unsure, put the whole name here)
- nombres: given names (best effort; "" if unsure)
- ci: cedula, DIGITS ONLY (strip V/E/dots/slashes). "" if none shown.
- age: number only. "" if none.
- age_unit: "years", "months", or "".
- sex: "M", "F", or "".
- status: one of "ingresado","alta","fallecido","por_localizar","". Map ALTA/EGRESO->"alta".
- origin: place/parish/town if shown (e.g. Caraballeda, Tanaguarena), else "".
- obs: anything else on the line (notes), else "".
- raw: the original line, verbatim.

Rules: transcribe faithfully, DO NOT invent people or cedulas. If a value is unreadable, use "". Keep every person, including children with no cedula."""


def extract_file(path: str | Path, source: str, hospital_hint: str = "") -> list[dict]:
    path = Path(path)
    md = ocr_document(path)
    if not md.strip():
        return []
    hosp_canon = nz.canonical_hospital(hospital_hint) or hospital_hint
    rows = gemma_jsonl(STRUCT_PROMPT.format(hospital=hosp_canon or "unknown") + "\n\nOCR TEXT:\n" + md,
                       max_tokens=16000)
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        apellidos = nz.clean_display(r.get("apellidos", ""))
        nombres = nz.clean_display(r.get("nombres", ""))
        if not (apellidos or nombres):
            continue
        ci = nz.normalize_ci(str(r.get("ci", "")))
        hosp = nz.canonical_hospital(str(r.get("obs", "")) + " " + hosp_canon) or hosp_canon
        rec = {
            "source": source,
            "source_type": "ocr_gemma",
            "hospital": hosp,
            "apellidos": apellidos.upper(),
            "nombres": nombres.upper(),
            "full_name": (apellidos + " " + nombres).strip().upper(),
            "name_key": nz.name_key(apellidos, nombres),
            "ci": ci,
            "age": str(r.get("age", "")).strip(),
            "age_unit": str(r.get("age_unit", "")).strip(),
            "sex": nz.parse_sex(str(r.get("sex", ""))),
            "origin": nz.clean_display(str(r.get("origin", ""))),
            "status": str(r.get("status", "")).strip().lower(),
            "obs": nz.clean_display(str(r.get("obs", ""))),
            "date": nz.find_date(str(r.get("raw", ""))),
            "row_raw": nz.clean_display(str(r.get("raw", ""))),
        }
        out.append(rec)
    return out


# folder name -> (source tag, hospital hint)
FOLDER_MAP = {
    "HOSPITAL DE CATIA": ("foto_catia", "Periferico de Catia"),
    "HOSPITAL LUCIANI CARACAS": ("foto_luciani", "Domingo Luciani"),
    "HOSPITAL PEREZ CARREÑO": ("foto_perez", "Perez Carreno"),
    "HOSPITAL UNIVERSITARIO CARACAS": ("foto_huc", "Universitario de Caracas"),
    "HOSPITAL VARGAS DE CARACAS": ("foto_vargas_ccs", "Vargas de Caracas"),
    "Personas en campo de golf Playa Los cocos": ("foto_albergue", "Albergue Campo de Golf"),
}

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}


def run(base: str, out_csv: str, limit_per_folder: int | None = None) -> None:
    base = Path(base)
    records: list[dict] = []
    cols = ["source", "source_type", "hospital", "apellidos", "nombres", "full_name",
            "name_key", "ci", "age", "age_unit", "sex", "origin", "status", "obs", "date", "row_raw"]
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or folder.name.startswith("__"):
            continue
        source, hint = FOLDER_MAP.get(folder.name, (folder.name.lower().replace(" ", "_"), folder.name))
        imgs = sorted([p for p in folder.iterdir() if p.suffix.lower() in IMG_EXT])
        if limit_per_folder:
            imgs = imgs[:limit_per_folder]
        for img in imgs:
            t = time.time()
            try:
                rows = extract_file(img, source, hint)
            except Exception as e:
                print(f"  ! {img.name}: {e}")
                continue
            records.extend(rows)
            withci = sum(1 for r in rows if r["ci"])
            print(f"  {folder.name}/{img.name}: {len(rows)} rows, {withci} with CI ({round(time.time()-t,1)}s)")
    Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(records)
    withci = sum(1 for r in records if r["ci"])
    print(f"\nTOTAL: {len(records)} rows from photos; {withci} with CI ({100*withci//max(1,len(records))}%) -> {out_csv}")


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "SISMO 2026 VZLA" / "SISMO 2026 VZLA")
    out = sys.argv[2] if len(sys.argv) > 2 else str(ROOT / "ingest" / "out" / "photos_records.csv")
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else None
    run(base, out, lim)
