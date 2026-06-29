"""Final pre-ship QA: where does EVERY distinct place in the dataset land on the map?
Deterministic geocode of all distinct origins -> bucket (in-zone / other city / unplaced),
then Gemma classifies the top UNPLACED places to surface La Guaira sectors we're missing."""
import sys, json
from collections import Counter
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
for p in ("ingest", "pipeline"): sys.path.insert(0, str(ROOT / p))
import store_db as db, geo_people as g          # noqa
from clients import gemma_jsonl                  # noqa

def inbox(lat, lng): return 10.54 <= lat <= 10.66 and -67.20 <= lng <= -66.55

def main():
    conn = db.connect(); z = g.load_zones(conn)
    people = inz = outz = unp = 0
    outby = Counter(); distinct = Counter()
    for (o,) in conn.execute("SELECT origins FROM persons"):
        people += 1
        arr = json.loads(o or "[]")
        loc = g.locate(g.order_origins(arr, z), z)
        if not loc: unp += 1
        elif inbox(loc["lat"], loc["lng"]): inz += 1
        else: outz += 1; outby[loc["zone"]] += 1
        for s in arr:
            if s: distinct[s] += 1
    print(f"PEOPLE {people}: in-zone {inz} ({100*inz//people}%) · other-city {outz} ({100*outz//people}%) · unplaced {unp} ({100*unp//people}%)")
    print("  other-city (correct, off the Catia La Mar map):", dict(outby.most_common(8)))
    placed = unp_freq = None
    placed = sum(1 for s in distinct if g.locate([s], z))
    unp_freq = Counter({s: c for s, c in distinct.items() if not g.locate([s], z)})
    print(f"DISTINCT PLACES {len(distinct)}: placed {placed} · unplaced {len(unp_freq)} ({sum(unp_freq.values())} people on unplaced strings)")
    top = unp_freq.most_common(45)
    items = "\n".join(f'{i}. "{s[:70]}"' for i, (s, c) in enumerate(top))
    prompt = ("Textos de ORIGEN (dónde estaba una persona) del terremoto La Guaira/Vargas 2026 que NO pudimos ubicar. "
              'Para cada uno responde JSONL: {"i":int,"tipo":"sector_laguaira"|"edificio"|"otra_ciudad"|"vago","lugar":""}. '
              "tipo=sector_laguaira SOLO si es un sector/zona reconocible de La Guaira/Vargas que deberíamos mapear; pon su nombre en 'lugar'.\nITEMS:\n" + items)
    try:
        rows = gemma_jsonl(prompt, max_tokens=2200)
    except Exception as e:
        print("  gemma error:", e); rows = []
    bytipo = Counter(); missing = Counter()
    for r in rows:
        if not isinstance(r, dict): continue
        bytipo[r.get("tipo")] += 1
        if r.get("tipo") == "sector_laguaira" and r.get("lugar"):
            i = r.get("i"); c = top[i][1] if isinstance(i, int) and i < len(top) else 0
            missing[r.get("lugar")] += c
    print("  Gemma on top-45 unplaced:", dict(bytipo))
    print("  >>> La Guaira sectors we may be MISSING (add to gazetteer):")
    for s, c in missing.most_common(12): print(f"      {s}  (~{c} people)")
    print("  (sample raw unplaced, top 8):")
    for s, c in top[:8]: print(f"      {c:5} {s[:50]!r}")

if __name__ == "__main__": main()
