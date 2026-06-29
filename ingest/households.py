"""Household / kinship layer — LINKS persons, never merges them.

Identity resolution answers "same person?"; this answers "related?". Signals,
strongest first:
  * co_mention   — people split out of ONE source report (shared base source_uid)
  * siblings     — share BOTH apellidos, and the apellido-pair is rare (small block)
  * kin          — share ONE rare apellido (e.g. a child carrying a parent's apellido)
Frequency-gated: only SMALL blocks link (rare surnames). Common surnames
(Rodríguez/González) never form households — the José Rodríguez principle again.

Output: persons.household_id + a household_edges table (who linked to whom, why).
Everything is "probable household", flagged, not asserted legal kinship.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
import store_db as db   # noqa: E402
import names as nm      # noqa: E402

MAX_BLOCK = 8           # a surname/pair shared by > this many people is too common to imply family


def _apellidos(display_name: str) -> list[str]:
    toks = nm._fold(display_name).split()
    return toks[-2:] if len(toks) >= 2 else (toks[-1:] if toks else [])


def rebuild(conn, max_block: int = MAX_BLOCK) -> dict:
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE persons ADD COLUMN household_id INTEGER")
    except Exception:
        pass
    cur.execute("CREATE TABLE IF NOT EXISTS household_edges(a INT, b INT, kind TEXT, weight INT)")
    cur.execute("DELETE FROM household_edges")
    cur.execute("UPDATE persons SET household_id=NULL")

    persons = cur.execute("SELECT person_id, display_name FROM persons").fetchall()
    parent = {p["person_id"]: p["person_id"] for p in persons}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    tok_block, pair_block = defaultdict(set), defaultdict(set)
    for p in persons:
        ap = _apellidos(p["display_name"])
        for t in set(ap):
            if len(t) > 2:
                tok_block[t].add(p["person_id"])
        if len(ap) >= 2:
            pair_block[" ".join(sorted(set(ap)))].add(p["person_id"])

    # co-mention: records that share a base source_uid (one report -> several people)
    base = defaultdict(set)
    for r in cur.execute("SELECT person_id, source, source_uid FROM records WHERE source_uid<>''"):
        base[(r["source"], (r["source_uid"] or "").split("#")[0])].add(r["person_id"])

    edges = []

    def link(group, kind, weight, cap=max_block):
        g = [x for x in group if x in parent]
        if not (2 <= len(g) <= cap):          # rarity gate (big groups = roundups/common names, not family)
            return
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                union(g[i], g[j]); edges.append((g[i], g[j], kind, weight))

    # Household membership = SURNAME signals only (rare-gated) -> precise, and immune
    # to co-mention chaining (a roundup post must never merge two families).
    for pair, g in pair_block.items():
        link(g, "siblings:" + pair, 3)
    for tok, g in tok_block.items():
        link(g, "kin:" + tok, 2)
    # Co-mention is recorded as a SOFT "reported-together" hint only (NOT unioned).
    for g in base.values():
        gg = [x for x in g if x in parent]
        if 2 <= len(gg) <= 4:
            for i in range(len(gg)):
                for j in range(i + 1, len(gg)):
                    edges.append((gg[i], gg[j], "co_mention", 1))

    comp = defaultdict(list)
    for pid in parent:
        comp[find(pid)].append(pid)
    hid, in_hh = 0, 0
    for members in comp.values():
        if len(members) > 1:
            hid += 1
            for m in members:
                cur.execute("UPDATE persons SET household_id=? WHERE person_id=?", (hid, m)); in_hh += 1

    cur.executemany("INSERT INTO household_edges(a,b,kind,weight) VALUES(?,?,?,?)", edges)
    conn.commit()
    return {"households": hid, "persons_in_households": in_hh, "edges": len(edges)}


def members(conn, person_id: int) -> list[dict]:
    """Surname-household members (precise) + people directly co-reported with them."""
    out, seen = [], set()

    def add(r, vinculo):
        if r["person_id"] in seen:
            return
        seen.add(r["person_id"])
        out.append({"person_id": f"P{r['person_id']:05d}", "display_name": r["display_name"],
                    "cedula": db._jget(r, "cis"), "estado": db._jget(r, "statuses"), "vinculo": vinculo})

    row = conn.execute("SELECT household_id FROM persons WHERE person_id=?", (person_id,)).fetchone()
    hid = row[0] if row else None
    if hid is not None:
        for r in conn.execute("SELECT person_id,display_name,cis,statuses FROM persons WHERE household_id=? AND person_id<>?",
                              (hid, person_id)):
            v = conn.execute("SELECT kind FROM household_edges WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND kind<>'co_mention' LIMIT 1",
                             (person_id, r["person_id"], r["person_id"], person_id)).fetchone()
            add(r, v[0] if v else "household")
    # directly co-reported-with (soft hint; not a household claim)
    for e in conn.execute("SELECT a,b FROM household_edges WHERE kind='co_mention' AND (a=? OR b=?)", (person_id, person_id)):
        other = e["b"] if e["a"] == person_id else e["a"]
        r = conn.execute("SELECT person_id,display_name,cis,statuses FROM persons WHERE person_id=?", (other,)).fetchone()
        if r:
            add(r, "reportado_junto")
    return out


if __name__ == "__main__":
    c = db.connect()
    print(rebuild(c))
