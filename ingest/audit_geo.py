"""Audit people-geocoding across the WHOLE list, then have GEMMA validate a sample.

1. Geocode every person's first origin (sector centroid + snap-to-nearest-building),
   in-memory, no Photon (so it's fast and doesn't hit rate limits) -> coverage + zone
   distribution + on-land check.
2. Gemma (Cerebras) judges a sample: given the free-text origin and the zone we assigned,
   is the zone a plausible match? -> error rate + flagged mismatches.
"""
from __future__ import annotations

import json, random, sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("ingest", "pipeline"):
    sys.path.insert(0, str(ROOT / p))
import store_db as db          # noqa: E402
import geo_people as g         # noqa: E402
from clients import gemma_jsonl  # noqa: E402


def main(sample_n: int = 500):
    conn = db.connect()
    zones = g.load_zones(conn)
    fp = g._load_footprints()
    # snap each zone centroid to the nearest real building ONCE (memoized -> fast for 19k origins)
    zone_pt = {}
    for zf, lat, lng in zones:
        sn = g.snap_to_building(lat, lng)
        zone_pt[zf] = (sn[0], sn[1], True) if sn else (lat, lng, False)

    cnt = Counter(); persons = 0
    for (o,) in conn.execute("SELECT origins FROM persons"):
        persons += 1
        arr = json.loads(o or "[]")
        if arr:
            cnt[arr[0]] += 1

    geo = {}
    for origin in cnt:
        a = g.locate([origin], zones)
        geo[origin] = (zone_pt[a["zone"]], a["zone"]) if a and a["zone"] in zone_pt else None

    placed = sum(c for o, c in cnt.items() if geo[o])
    on_building = sum(c for o, c in cnt.items() if geo[o] and geo[o][0][2])
    byzone = Counter()
    for o, c in cnt.items():
        if geo[o]:
            byzone[geo[o][1]] += c
    print(f"footprints={len(fp)}  people={persons}  placed={placed} ({100*placed//persons}%)  "
          f"on a real building(land)={on_building} ({100*on_building//max(1,placed)}% of placed)  "
          f"distinct_origins={len(cnt)}")
    print("people per zone (top 14):")
    for z, c in byzone.most_common(14):
        print(f"   {z:26} {c}")
    novalue = sorted(((c, o) for o, c in cnt.items() if not geo[o]), reverse=True)[:6]
    print(f"top UN-placed origins (no zone token; {sum(c for o,c in cnt.items() if not geo[o])} people):")
    for c, o in novalue:
        print(f"   {c:5}  {o[:60]!r}")

    # ---- GEMMA validates a sample (does the assigned zone match the origin text?) ----
    placed_origins = [o for o in cnt if geo[o]]
    random.seed(7)
    sample = random.sample(placed_origins, min(sample_n, len(placed_origins)))
    verdicts = Counter(); wrong = []
    B = 25
    for i in range(0, len(sample), B):
        chunk = sample[i:i + B]
        items = "\n".join(f'{j}. origin="{o[:80]}" zona_asignada="{geo[o][1]}"' for j, o in enumerate(chunk))
        prompt = ("Eres validador. Para cada ítem recibes el ORIGEN en texto libre (dónde estaba una persona "
                  "durante el terremoto de La Guaira/Vargas 2026) y la ZONA que asignó nuestro sistema. "
                  'Responde JSONL, un objeto por línea: {"i":int,"v":"ok"|"wrong"|"unsure"}. '
                  '"wrong" SOLO si la zona claramente contradice el origen (otro municipio/sector). '
                  'Origen vago + zona razonable = "ok".\nITEMS:\n' + items)
        try:
            rows = gemma_jsonl(prompt, max_tokens=1600)
        except Exception as e:
            print("  gemma batch error:", str(e)[:80]); continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = str(r.get("v", "")).lower(); idx = r.get("i")
            verdicts[v] += 1
            if v == "wrong" and isinstance(idx, int) and idx < len(chunk):
                wrong.append((chunk[idx], geo[chunk[idx]][1]))
    total_v = sum(verdicts.values()) or 1
    print(f"\nGEMMA validation (sample {len(sample)}): {dict(verdicts)}  "
          f"=> {100*verdicts.get('ok',0)//total_v}% ok, {100*verdicts.get('wrong',0)//total_v}% wrong")
    print("sample of Gemma-flagged WRONG (origin -> zone):")
    for o, z in wrong[:15]:
        print(f"   {o[:58]!r} -> {z}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 500)
