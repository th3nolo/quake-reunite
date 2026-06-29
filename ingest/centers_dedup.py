"""Conservative cross-source center de-duplication — the José Rodríguez rule for places.

The risk: the SAME physical center listed on two directories (acopiovzla +
centrosacopio) under slightly different names => it shows up twice.

The trap (proven by audit of the real data): apparent "duplicates" are mostly
DISTINCT drop-off points of one organisation — "Rotaract Santo Ángel — C / — T / — L",
"Sociedad Civil — UCAT / — UNET" (1.6 km apart). They share a name prefix and even a
phone, but they are different places. Merging them would be wrong. And 153/253 centers
sit on a municipality centroid (address geocode failed), so coordinates can't tell them
apart either.

So we merge ONLY when confident:
  * different sources (same-source repeats are legit distinct drop-offs),
  * same municipality,
  * near-identical names (token-set Jaccard >= THRESHOLD), AND
  * they share a DISTINCTIVE token (not just generic words like "sociedad", "civil",
    "comando", "acopio") — otherwise "Sociedad civil" would swallow every civil-society
    center in town.
Phone and coordinates are deliberately NOT merge keys (shared across drop-offs / centroids).

Merge is SOFT: the duplicate row keeps all its data and gets `canonical_id` pointing at the
survivor; the survivor absorbs the union of needs tags + the source list + any missing
address/phone/hours. Nothing is deleted, so it's reversible and idempotent — re-running
recomputes from scratch (reset -> recluster), and a later source refresh that re-creates a
duplicate row is simply re-pointed on the next pass. The API shows only `canonical_id IS NULL`.
"""
from __future__ import annotations

import json, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
import store_db as db   # noqa: E402
import names as nm      # noqa: E402

THRESHOLD = 0.80        # name token-set Jaccard required to call two centers the same place

# Words too generic to imply "same center" on their own (operator labels, place-type nouns).
GENERIC = {
    "sociedad", "civil", "gobierno", "gobernacion", "alcaldia", "comando", "venezuela",
    "vzla", "con", "los", "las", "del", "para", "vecinos", "rescatistas", "acopio",
    "centro", "centros", "punto", "puntos", "recoleccion", "donacion", "donaciones",
    "ayuda", "ayudas", "voluntarios", "voluntariado", "comunidad", "fundacion", "grupo",
    "club", "casa", "sede", "local", "nacional", "comercial", "operacion", "todos",
}


def _toks(s: str) -> set[str]:
    return {t for t in nm._fold(s or "").split() if len(t) > 2}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if (a or b) else 0.0


def reconcile(conn, threshold: float = THRESHOLD) -> dict:
    db.ensure_center_columns(conn)
    conn.execute("UPDATE centers SET canonical_id=NULL, merged_sources=NULL")
    rows = [dict(r) for r in conn.execute(
        "SELECT center_id,name,municipality,source,address,phone,hours,needs_tags FROM centers")]
    by_id = {r["center_id"]: r for r in rows}

    parent = {r["center_id"]: r["center_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # keep the lower id as cluster root (stable)

    block = defaultdict(list)
    for r in rows:
        block[nm._fold(r["municipality"] or "")].append(r)

    edges = 0
    for mun, g in block.items():
        if not mun:
            continue                       # don't cluster centers with no municipality
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                a, b = g[i], g[j]
                if a["source"] == b["source"]:
                    continue               # same-source repeats = distinct drop-offs
                ta, tb = _toks(a["name"]), _toks(b["name"])
                if _jaccard(ta, tb) < threshold:
                    continue
                if not ((ta & tb) - GENERIC):  # shared tokens must include something distinctive
                    continue
                union(a["center_id"], b["center_id"]); edges += 1

    clusters = defaultdict(list)
    for cid in parent:
        clusters[find(cid)].append(cid)

    merged = n_clusters = 0
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        n_clusters += 1
        # survivor = most complete (address, then phone), lowest id as tiebreak
        canon = max(members, key=lambda m: (bool(by_id[m]["address"]), bool(by_id[m]["phone"]), -m))
        tags, srcs = set(), set()
        fill = {"address": by_id[canon]["address"], "phone": by_id[canon]["phone"], "hours": by_id[canon]["hours"]}
        for m in members:
            r = by_id[m]
            try:
                tags |= set(json.loads(r["needs_tags"] or "[]"))
            except Exception:
                pass
            if r["source"]:
                srcs.add(r["source"])
            for k in fill:
                if not fill[k] and r[k]:
                    fill[k] = r[k]
            if m != canon:
                conn.execute("UPDATE centers SET canonical_id=? WHERE center_id=?", (canon, m))
                merged += 1
        conn.execute(
            "UPDATE centers SET needs_tags=?, merged_sources=?, address=?, phone=?, hours=? WHERE center_id=?",
            (json.dumps(sorted(tags)), ",".join(sorted(srcs)), fill["address"], fill["phone"], fill["hours"], canon))
    conn.commit()
    return {"pairs_linked": edges, "clusters": n_clusters, "merged_away": merged, "visible": len(rows) - merged}


if __name__ == "__main__":
    print(reconcile(db.connect()))
