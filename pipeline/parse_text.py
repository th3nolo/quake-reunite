"""Parse the text-layer sources (no OCR needed) into unified records.

Sources:
  consolidado_full.txt    NUM | APELLIDOS | NOMBRES | CI | EDAD | SEXO | PROCEDENCIA | HOSPITAL | FECHA
  registro_pdf.txt        N° | HOSPITAL | APELLIDOS Y NOMBRES | EDAD   (hand master, also in 2 DOCX)
  huc_report.txt          official HUC report: deceased + injured tables (NOMBRE first, then APELLIDO)
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from normalize import (
    canonical_hospital, clean_display, find_date, fold, looks_like_ci,
    looks_like_hospital, name_key, normalize_ci, parse_age, parse_sex,
)

RAW = Path(__file__).resolve().parent.parent / "data" / "raw_text"


def _rec(**kw):
    base = dict(source="", source_type="", row_raw="", apellidos="", nombres="",
               ci="", age="", age_unit="", sex="", origin="", hospital_raw="",
               hospital="", status="ingresado", obs="", date="")
    base.update(kw)
    base["full_name"] = clean_display(f"{base['apellidos']} {base['nombres']}")
    base["name_key"] = name_key(base["apellidos"], base["nombres"])
    if not base["hospital"]:
        base["hospital"] = canonical_hospital(base["hospital_raw"]) or clean_display(base["hospital_raw"])
    return base


# ---------------------------------------------------------------------------
# Consolidated PDF (the richest source: has CI, sex, origin, date)
# ---------------------------------------------------------------------------

def parse_consolidado(path: Path, source: str) -> list[dict]:
    records = []
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if "APELLIDOS" in line or line.strip().startswith("NUM"):
            continue
        if not (looks_like_hospital(line) or "HOSPITAL" in line or "HOPITAL" in line):
            continue
        toks = re.split(r"\s{2,}", line.strip())
        toks = [t for t in toks if t]
        if len(toks) < 2:
            continue
        # Classify tokens
        hospital_raw = ""
        date = find_date(line)
        ci = ""
        age = age_unit = sex = origin = ""
        name_toks = []
        leftover = []
        for t in toks:
            tf = t.strip()
            if looks_like_hospital(tf) and not hospital_raw:
                hospital_raw = tf
            elif looks_like_ci(tf) and not ci:
                ci = normalize_ci(tf)
            elif re.fullmatch(r"[MF]", fold(tf)) and not sex:
                sex = fold(tf)
            elif re.fullmatch(r"\d{1,3}", tf) and not age:
                age = tf
            elif re.fullmatch(r"\d{1,3}\s*(M|F|MESES|MES|ANOS|AÑOS)?", fold(tf)) and not age:
                a, u = parse_age(tf)
                age, age_unit = a, u
                # trailing sex letter
                msex = re.search(r"\b([MF])$", fold(tf))
                if msex and not sex:
                    sex = msex.group(1)
            else:
                leftover.append(tf)
        # remove date token from leftover
        leftover = [t for t in leftover if not find_date(t)]
        # First two leftover text tokens are APELLIDOS then NOMBRES.
        # Remaining uppercase leftovers are origin (procedencia).
        text_toks = [t for t in leftover if re.search(r"[A-Za-zÑñ]", t)]
        apellidos = text_toks[0] if text_toks else ""
        nombres = text_toks[1] if len(text_toks) > 1 else ""
        if len(text_toks) > 2:
            origin = " ".join(text_toks[2:])
        if not (apellidos or ci):
            continue
        records.append(_rec(
            source=source, source_type="pdf_text", row_raw=line.strip(),
            apellidos=apellidos, nombres=nombres, ci=ci, age=age, age_unit=age_unit,
            sex=sex, origin=origin, hospital_raw=hospital_raw, date=date,
        ))
    return records


# ---------------------------------------------------------------------------
# Hand master registry: from the 44-page PDF (and identical in 2 DOCX).
# Layout per row:  N°  HOSPITAL/SECTION   APELLIDOS Y NOMBRES   EDAD
# Section headers (CRUZ ROJA, PERIFÉRICO DE CATIA...) switch the active hospital.
# ---------------------------------------------------------------------------

def parse_registro_pdf(path: Path, source: str) -> list[dict]:
    records = []
    cur_hospital = ""
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("REGISTRO MAESTRO") or line.startswith("Para buscar") or line.startswith("N°"):
            continue
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        # Fields are separated by 2+ spaces: <Hospital>  <NAME>  <age>
        cols = re.split(r"\s{2,}", m.group(2).strip())
        cols = [c for c in cols if c]
        if not cols:
            continue
        hosp_raw = cols[0]
        hosp = canonical_hospital(hosp_raw)
        # Trailing age column
        age = ""
        if len(cols) >= 3 and re.fullmatch(r"\d{1,3}(\s*(AÑOS|ANOS|MESES))?", cols[-1], re.IGNORECASE):
            age = re.match(r"\d{1,3}", cols[-1]).group(0)
            name = " ".join(cols[1:-1])
        else:
            name = " ".join(cols[1:])
            am = re.search(r"\s(\d{1,3})\s*$", name)
            if am:
                age = am.group(1)
                name = name[: am.start()].strip()
        if hosp:
            cur_hospital = hosp
        else:
            hosp = cur_hospital or clean_display(hosp_raw)
        name = clean_display(name)
        if not name:
            continue
        records.append(_rec(
            source=source, source_type="registro", row_raw=line,
            apellidos=name, nombres="", age=age, hospital=hosp,
            hospital_raw=hosp_raw,
        ))
    return records


# ---------------------------------------------------------------------------
# HUC official report: deceased + injured. NOMBRE first, APELLIDO second.
# ---------------------------------------------------------------------------

def parse_huc(path: Path, source: str) -> list[dict]:
    records = []
    status = "herido"
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        low = fold(line)
        if "FALLECID" in low or "CUADRO DE FALLECIDOS" in low:
            status = "fallecido"
        if "HERIDOS" in low or "LESIONADOS" in low:
            status = "herido"
        m = re.match(r"^(\d+)\s+(.*)$", line)
        if not m:
            continue
        body = m.group(2)
        toks = re.split(r"\s{2,}", body.strip())
        toks = [t for t in toks if t]
        if not toks:
            continue
        name = toks[0]
        age = ""
        ci = ""
        origin = ""
        obs = ""
        rest = toks[1:]
        for t in rest:
            a, u = parse_age(t)
            if a and not age and re.search(r"\d", t) and ("AÑOS" in fold(t) or "ANOS" in fold(t) or re.fullmatch(r"\d{1,3}", t.strip())):
                age = a
            elif looks_like_ci(t) and not ci:
                ci = normalize_ci(t)
            elif re.fullmatch(r"\d{6,9}", t.strip()) and not ci:
                ci = normalize_ci(t)
            elif re.search(r"[A-Za-z]", t):
                if not origin:
                    origin = t
                else:
                    obs = (obs + " " + t).strip()
        records.append(_rec(
            source=source, source_type="huc", row_raw=line,
            apellidos=name, nombres="", age=age, ci=ci, origin=origin, obs=obs,
            status=status, hospital="Hospital Universitario de Caracas (HUC)",
            hospital_raw="HOSPITAL UNIVERSITARIO DE CARACAS",
        ))
    return records


def parse_all_text() -> list[dict]:
    out = []
    out += parse_consolidado(RAW / "consolidado_full.txt", "consolidado_full")
    out += parse_registro_pdf(RAW / "registro_pdf.txt", "registro")
    out += parse_huc(RAW / "huc_report.txt", "huc_report")
    for idx, record in enumerate(out, start=1):
        record["record_id"] = f"R{idx:05d}"
    return out


if __name__ == "__main__":
    import json, sys
    recs = parse_all_text()
    by_src = {}
    for r in recs:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print("records by source:", by_src, "total:", len(recs), file=sys.stderr)
    if "--sample" in sys.argv:
        for r in recs[:25]:
            print(json.dumps({k: r[k] for k in ("source","apellidos","nombres","ci","age","sex","origin","hospital")}, ensure_ascii=False))
    else:
        for r in recs:
            print(json.dumps(r, ensure_ascii=False))
