"""Turn the OCR workflow output (data/ocr/photos.json) into unified records.

The workflow returns:
  {"images":[{"file","folder","hospital","hospital_header","list_kind",
              "confidence","notes","rows":[{raw,apellidos,nombres,ci,edad,
              sexo,cama,direccion,observacion,estado}], ...}], "total_rows"}
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from normalize import canonical_hospital, clean_display, normalize_ci, parse_age
from parse_text import _rec

OCR_JSON = Path(__file__).resolve().parent.parent / "data" / "ocr" / "photos.json"

FOLDER_CODE = {
    "HOSPITAL DE CATIA": "foto_catia",
    "HOSPITAL LUCIANI CARACAS": "foto_luciani",
    "HOSPITAL PEREZ CARREÑO": "foto_perez",
    "HOSPITAL VARGAS DE CARACAS": "foto_vargas",
    "Personas en campo de golf Playa Los cocos": "foto_albergue",
}

STATUS_MAP = {
    "fallecido": "fallecido", "fallecida": "fallecido", "muerto": "fallecido",
    "herido": "herido", "lesionado": "herido", "albergado": "albergado",
    "ingresado": "ingresado",
}


def _date_from_filename(fn: str) -> str:
    m = re.search(r"(20\d\d)-(\d\d)-(\d\d)", fn)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    m = re.search(r"-(20\d\d)(\d\d)(\d\d)-", fn)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return ""


def parse_photos(path: Path = OCR_JSON) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    images = data.get("images", data if isinstance(data, list) else [])
    records = []
    for img in images:
        folder = img.get("folder", "")
        hospital = img.get("hospital", "") or canonical_hospital(img.get("hospital_header", ""))
        source = FOLDER_CODE.get(folder, "foto")
        date = _date_from_filename(img.get("file", ""))
        shelter = source == "foto_albergue"
        for row in img.get("rows", []):
            apellidos = clean_display(row.get("apellidos", ""))
            nombres = clean_display(row.get("nombres", ""))
            raw = clean_display(row.get("raw", ""))
            if not (apellidos or nombres):
                # fall back to the raw line (strip leading numbering/bed)
                name = re.sub(r"^\s*(cama\s*#?\s*\d+|#\s*\d+|\d+)\s*", "", raw, flags=re.I)
                apellidos = clean_display(name)
            if not (apellidos or nombres):
                continue
            age, age_unit = parse_age(row.get("edad", ""))
            est = (row.get("estado", "") or "").strip().lower()
            status = STATUS_MAP.get(est, "albergado" if shelter else "ingresado")
            records.append(_rec(
                source=source, source_type="photo", row_raw=raw or f"{apellidos} {nombres}".strip(),
                apellidos=apellidos, nombres=nombres,
                ci=normalize_ci(row.get("ci", "")),
                age=age, age_unit=age_unit,
                sex=(row.get("sexo", "") or "").strip().upper()[:1] if (row.get("sexo", "") or "").strip().upper()[:1] in ("M", "F") else "",
                origin=clean_display(row.get("direccion", "")),
                obs=clean_display(row.get("observacion", "")),
                status=status,
                hospital=hospital,
                hospital_raw=img.get("hospital_header", "") or hospital,
                date=date,
                bed=clean_display(row.get("cama", "")),
            ))
    return records


if __name__ == "__main__":
    recs = parse_photos()
    from collections import Counter
    print("photo records:", len(recs))
    print("by source:", Counter(r["source"] for r in recs))
    for r in recs[:15]:
        print(" ", r["source"], "|", repr(r["full_name"]), "| CI", r["ci"], "| edad", r["age"], "|", r["hospital"])
