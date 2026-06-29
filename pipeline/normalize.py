"""Normalization helpers for the SISMO 2026 VZLA patient registry.

Goal: take messy rows from many lists (consolidated PDF, hand registry, HUC
official report, OCR'd photos) and produce comparable fields so the same person
can be found across lists. Everything keeps a raw copy for human audit.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Text folding
# ---------------------------------------------------------------------------

def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def fold(text: str) -> str:
    """ASCII-fold + uppercase + collapse spaces. Used for matching only."""
    if not text:
        return ""
    t = strip_accents(text).upper()
    t = t.replace("Ñ", "N")  # already folded by strip_accents, kept explicit
    t = re.sub(r"[^A-Z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def clean_display(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# ---------------------------------------------------------------------------
# Hospital canonicalization
# ---------------------------------------------------------------------------
# Each tuple: (canonical name, list of substring patterns matched on folded text).
# Order matters: more specific first. NOTE: "Vargas de La Guaira" and
# "Vargas de Caracas" are DIFFERENT hospitals and must not be merged.
HOSPITAL_RULES: list[tuple[str, list[str]]] = [
    ("Hospital Pérez Carreño", ["PEREZ CARRENO", "PEREZ CARRENNO", "PEREZ CARREÑO"]),
    ("Hospital Domingo Luciani (El Llanito)",
     ["DOMINGO LUCIANI", "LUCIANI", "EL LLANITO"]),
    ("Hospital José María Vargas (La Guaira)",
     ["JOSE MARIA VARGAS LA GUAIRA", "JM VARGAS LA GUAIRA", "VARGAS LA GUAIRA",
      "JOSE MARIA VARGAS"]),
    ("Hospital Vargas de Caracas", ["VARGAS DE CARACAS"]),
    ("Hospital Universitario de Caracas (HUC)",
     ["UNIVERSITARIO DE CARACAS", "UNIVERSITARIO CARACAS", "HUC", "UNIVERSITARIO"]),
    ("Hospital Ricardo Baquero González (Periférico de Catia)",
     ["RICARDO BAQUERO", "BAQUERO GONZALEZ", "PERIFERICO DE CATIA", "PERIFERICO CATIA"]),
    ("Hospital J.M. de los Ríos (Niños)",
     ["JM DE LOS RIOS", "J M DE LOS RIOS", "DE LOS RIOS", "DE NINOS", "DE NIÑOS", "HOSP DE NINOS"]),
    ("Cruz Roja", ["CRUZ ROJA"]),
]


def canonical_hospital(raw: str) -> str:
    """Map a raw hospital/location label to a canonical hospital name.

    Returns "" if nothing matched (caller keeps raw)."""
    f = fold(raw)
    if not f:
        return ""
    for canonical, patterns in HOSPITAL_RULES:
        for p in patterns:
            if p in f:
                return canonical
    return ""


HOSPITAL_TOKEN_RE = re.compile(
    r"HOSP|HOPITAL|CLINICA|CLÍNICA|CRUZ ROJA|DE LOS RIOS|DE NIÑOS|DE NINOS|PERIFERICO|PERIFÉRICO",
    re.IGNORECASE,
)


def looks_like_hospital(token: str) -> bool:
    return bool(HOSPITAL_TOKEN_RE.search(token))


# ---------------------------------------------------------------------------
# CI (cédula de identidad) normalization
# ---------------------------------------------------------------------------

def normalize_ci(raw: str) -> str:
    """Keep digits only. Venezuelan CI is up to 8 digits (sometimes 7)."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits


CI_TOKEN_RE = re.compile(r"^\d{5,9}\)?$")


def looks_like_ci(token: str) -> bool:
    return bool(CI_TOKEN_RE.match(token.strip()))


# ---------------------------------------------------------------------------
# Age / sex
# ---------------------------------------------------------------------------

def parse_age(raw: str) -> tuple[str, str]:
    """Return (age_value, unit) where unit in {years, months, ''}.
    Examples: '39' -> ('39','years'); '3 MESES' -> ('3','months');
    '9 M' is ambiguous (month vs male) -> ('9','') leaving M to sex."""
    if not raw:
        return "", ""
    f = fold(raw)
    m = re.search(r"(\d{1,3})\s*(MESES|MES|M\b)?", f)
    if not m:
        return "", ""
    val = m.group(1)
    unit = ""
    if "MESES" in f or re.search(r"\bMES\b", f):
        unit = "months"
    elif "ANOS" in f or "ANO" in f or "AÑOS" in f:
        unit = "years"
    return val, unit


SEX_RE = re.compile(r"^[MF]$")


def parse_sex(token: str) -> str:
    t = fold(token)
    if t in ("M", "F"):
        return t
    return ""


DATE_RE = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}")


def find_date(text: str) -> str:
    m = DATE_RE.search(text)
    return m.group(0).replace(" ", "") if m else ""


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------

NAME_NOISE_RE = re.compile(r"\((menor|nino|nina|adulto|na)\)", re.IGNORECASE)


def split_variants(name: str) -> list[str]:
    """'WUIKMER/WILMER' -> ['WUIKMER','WILMER']; 'RAMOS/SILVA' -> both."""
    parts = re.split(r"\s*/\s*", name)
    return [p for p in (clean_display(p) for p in parts) if p]


def name_key(apellidos: str, nombres: str) -> str:
    """Order-independent folded token set, for blocking/matching.
    Handles that some lists put surname first and some put name first by
    sorting all tokens."""
    toks = []
    for chunk in (apellidos, nombres):
        for v in split_variants(chunk or ""):
            toks.extend(fold(v).split())
    toks = [t for t in toks if t and t not in ("DE", "LA", "DEL", "LOS", "Y", "DI")]
    return " ".join(sorted(set(toks)))


def name_tokens(apellidos: str, nombres: str) -> set[str]:
    return set(name_key(apellidos, nombres).split())
