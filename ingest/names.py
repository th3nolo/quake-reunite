"""Name handling to maximize match odds (misspellings, ordering, family derivation).

Venezuelan full name = nombre(s) + apellido_paterno + apellido_materno.
Registries often hold a partial or misspelled name (a stranger reports an
unconscious person), so we search several plausible variants, and we can DERIVE a
young person's full surname from their parents' surnames.
"""
from __future__ import annotations

import re
import unicodedata


def _fold(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s.lower()).strip()


def spelling_variants(token: str) -> set[str]:
    """Common Venezuelan-Spanish spelling confusions for one token."""
    t = _fold(token)
    out = {t}
    if len(t) < 2:
        return out
    if t.startswith("h"):
        out.add(t[1:])              # Hernandez -> Ernandez (silent H dropped)
    elif t[0] in "aeiou":
        out.add("h" + t)            # Ernandez -> Hernandez (only vowel-initial; avoids 'hrodriguez')
    for a, b in (("v", "b"), ("b", "v"), ("s", "z"), ("z", "s"), ("y", "i"),
                 ("j", "g"), ("ll", "y"), ("rr", "r")):
        if a in t:
            out.add(t.replace(a, b))
    return {v for v in out if len(v) > 1}


def apellido_paterno(full_name: str) -> str:
    """Heuristic: last two tokens are the apellidos; apellido_paterno = 2nd-to-last."""
    toks = _fold(full_name).split()
    return toks[-2] if len(toks) >= 2 else (toks[-1] if toks else "")


def derive_child_name(child_nombres: str, father_full: str = "", mother_full: str = "") -> str:
    """Ana + father 'Pedro Perez ...' + mother 'Maria Mora Sosa'
    -> 'ana perez mora'  (apellido_paterno del padre + apellido_paterno de la madre)."""
    ap_p = apellido_paterno(father_full)
    ap_m = apellido_paterno(mother_full)
    return " ".join(x for x in [_fold(child_nombres), ap_p, ap_m] if x).strip()


def name_variants(full_name: str, max_n: int = 10) -> list[str]:
    """Plausible spellings + apellido-order swap for one name (folded)."""
    toks = _fold(full_name).split()
    if not toks:
        return []
    out = [" ".join(toks)]
    # swap the two apellidos first (high value)
    if len(toks) >= 3:
        s = toks[:]; s[-1], s[-2] = s[-2], s[-1]
        out.append(" ".join(s))
    # spelling variants on the APELLIDO tokens (last 2) — the discriminative, error-prone part;
    # apellidos last-first so the best variants survive any cap.
    ap_idx = list(range(max(1, len(toks) - 2), len(toks)))
    for i in reversed(ap_idx):
        for v in spelling_variants(toks[i]):
            if v != toks[i]:
                nt = toks[:]; nt[i] = v
                out.append(" ".join(nt))
    # dedupe preserve order
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return uniq[:max_n]


def search_terms(full_name: str, ci: str = "", relatives: dict | None = None, max_terms: int = 16) -> list[str]:
    """All query strings to try against a source for one person:
    cédula formats + name spelling variants + family-derived name."""
    terms: list[str] = []
    d = "".join(c for c in (ci or "") if c.isdigit())
    if d:
        rev = d[::-1]
        dotted = ".".join(rev[i:i + 3] for i in range(0, len(rev), 3))[::-1]
        terms += [d, dotted, "V-" + dotted]
    terms += name_variants(full_name)
    if relatives:  # {"nombres": "...", "father": "...", "mother": "..."}
        derived = derive_child_name(relatives.get("nombres", "") or full_name,
                                    relatives.get("father", ""), relatives.get("mother", ""))
        if derived:
            terms += [derived] + name_variants(derived, max_n=4)
    seen, uniq = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t); uniq.append(t)
    return uniq[:max_terms]


if __name__ == "__main__":
    import json
    print("name_variants('Ana Perez Mora'):")
    print(" ", name_variants("Ana Perez Mora"))
    print("derive (Ana; padre Perez, madre Maria Mora Sosa):")
    print(" ", derive_child_name("Ana", "Pedro Perez Lopez", "Maria Mora Sosa"))
    print("search_terms for Ana w/ family + cédula:")
    print(json.dumps(search_terms("Ana Perez Mora", "12345678",
          {"nombres": "Ana", "father": "Pedro Perez Lopez", "mother": "Maria Mora Sosa"}),
          ensure_ascii=False, indent=1))
