"""Autonomous maintainer loop.

Per cycle: find due sources (cadence OR content changed) -> run the right
connector -> if anything changed, re-aggregate (resolve) and reload the API.
Merges stay strong-ID-only (that's enforced in resolve.py); name matches go to
the review queue, never auto-applied. Gemma can re-order the due list (optional).

  python maintainer/loop.py --once
  python maintainer/loop.py --loop --interval 600
"""
from __future__ import annotations
import csv, os, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))

import registry as reg          # noqa: E402
import load_to_db                # noqa: E402  (streams CSVs -> SQLite, dedup in backend)
import store_db as db            # noqa: E402
import centers_refresh           # noqa: E402  (agent re-reads center source pages)
from ratelimit import GLOBAL     # noqa: E402

API_RELOAD_URL = os.environ.get("API_RELOAD_URL", "http://127.0.0.1:8080/admin/reload")
ING_OUT = ROOT / "ingest" / "out"


def _log(msg: str) -> None:
    print(f"[maintainer {reg.now_iso()}] {msg}", flush=True)


def _reextract_web(web_sources: list[dict]) -> None:
    from extract_web import extract_web
    persons, centers = [], []
    for s in web_sources:
        try:
            p = Path(s["path"])
            if not p.exists():
                _log(f"  web {s['id']}: cache missing {p} (refresh via firecrawl)"); continue
            GLOBAL.acquire(8000)
            res = extract_web(p.read_text(encoding="utf-8", errors="replace"), source=s["id"],
                              url=s.get("refresh_url", ""))
            persons += res["persons"]; centers += res["centers"]
            _log(f"  web {s['id']}: {len(res['persons'])} persons, {len(res['centers'])} centers")
        except Exception as e:   # isolate: one web source must not abort the whole cycle
            _log(f"  ! web {s['id']} failed: {e}")
    if persons:
        with (ING_OUT / "web_persons.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(persons[0].keys())); w.writeheader(); w.writerows(persons)
    if centers:
        with (ING_OUT / "web_centers.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(centers[0].keys())); w.writeheader(); w.writerows(centers)


def _reextract_photos(src: dict) -> None:
    from extract_docs import run as run_docs
    base = ROOT / src["path"]
    run_docs(str(base), str(ING_OUT / "photos_records.csv"))


def _ingest_video(src: dict) -> None:
    from extract_video import ingest_urls
    ingest_urls(src.get("urls", []), out_csv=str(ING_OUT / "video_persons.csv"))


def gemma_prioritize(due: list[dict]) -> list[dict]:
    """Optional: let Gemma order the due sources by likely freshness/impact."""
    if os.environ.get("MAINTAINER_GEMMA_PRIORITIZE") != "1" or len(due) < 2:
        return due
    try:
        from clients import gemma_chat
        ids = [s["id"] for s in due]
        ans = gemma_chat("Order these earthquake-data source ids by likely freshness and "
                         "life-safety impact (most first). Reply as a comma list, ids only:\n"
                         + ", ".join(ids), max_tokens=200)
        order = [x.strip() for x in ans.replace("\n", ",").split(",") if x.strip() in ids]
        ranked = [s for i in order for s in due if s["id"] == i]
        return ranked + [s for s in due if s not in ranked] or due
    except Exception as e:
        _log(f"  prioritize skipped: {e}")
        return due


def run_once() -> dict:
    sources = [s for s in reg.load_sources() if s.get("enabled")]
    state = reg.load_state()
    due = [s for s in sources if reg.is_due(s, state)]
    due = gemma_prioritize(due)
    _log(f"{len(due)}/{len(sources)} sources due: {[s['id'] for s in due]}")
    changed = False
    web_due = [s for s in due if s["kind"] == "web_cache"]
    if web_due:
        _reextract_web([s for s in sources if s["kind"] == "web_cache" and s.get("enabled")])
        changed = True
        for s in web_due:
            reg.mark_done(s, state)
    for s in due:
        try:
            if s["kind"] == "photos":
                _reextract_photos(s); changed = True; reg.mark_done(s, state)
            elif s["kind"] == "pdf":
                changed = True; reg.mark_done(s, state)  # aggregate re-reads PDFs
            elif s["kind"] == "video":
                _ingest_video(s); changed = True; reg.mark_done(s, state)
            elif s["kind"] == "vr_api":
                from extract_vr_api import pull as vr_pull
                since = state.get(s["id"], {}).get("last_run")  # ISO
                # NEW reports: delta-only (the API's `since` = created-since -> cheap).
                n = vr_pull(statuses=("buscando",), since=since, clean=True, merge=True)
                # STATUS CHANGES: the found/safe lists are SMALL (~6k vs ~46k missing),
                # so full-pull them every cycle. add_record is idempotent on source_uid,
                # so a person whose ficha flipped buscando->encontrado gets updated, cheaply.
                n += vr_pull(statuses=("encontrado", "a_salvo"), since=None, clean=True, merge=True)
                _log(f"  vr_api: {n} records (new since={since or 'full'} + full found-set)")
                changed = changed or n > 0
                reg.mark_done(s, state)
        except Exception as e:
            _log(f"  ! {s['id']} failed: {e}")
    if changed:
        # delta-only: re-reads sources but add_record is idempotent on source_uid,
        # so only new/changed rows are written (no full re-dedup, no DB "blink").
        summary = load_to_db.rebuild(reset=False)
        _reload_api()
    else:
        summary = {"changed": False}
        _log("nothing changed")

    # Center freshness: agent re-reads each source page (TTL-throttled to ~3h), full
    # geocode in the background. Runs every cycle, independent of person changes.
    try:
        cconn = db.connect()
        cr = centers_refresh.refresh_for_query(cconn, max_new_geocode=None)
        active = [r for r in cr if not r.get("throttled")]
        if active:
            import centers_dedup
            cd = centers_dedup.reconcile(cconn)   # re-merge any dupes the refresh re-created
            _log(f"  centers refresh: {active}  dedup: {cd}")
            cconn.close()
            _reload_api()
        else:
            cconn.close()
    except Exception as e:
        _log(f"  ! centers refresh failed: {e}")

    # regenerate the served search page from the fresh DB (so buscador.html stays current)
    try:
        sys.path.insert(0, str(ROOT / "pipeline"))
        import build_db
        bd = build_db.export()
        _log(f"  page rebuilt: {bd['people']} people, {bd['bytes'] // 1024}KB")
    except Exception as e:
        _log(f"  ! page rebuild failed: {e}")

    # observability: persist a snapshot of the index this cycle (durable time-series)
    try:
        mc = db.connect()
        sec = summary.get("seconds", 0.0) if isinstance(summary, dict) else 0.0
        m = db.record_cycle(mc, changed=changed, seconds=sec, note=f"due={[s['id'] for s in due]}")
        mc.close()
        _log(f"  metrics recorded: {m}")
    except Exception as e:
        _log(f"  ! metrics failed: {e}")

    reg.save_state(state)
    return summary


def _reload_api() -> None:
    try:
        req = urllib.request.Request(API_RELOAD_URL, data=b"", method="POST")
        urllib.request.urlopen(req, timeout=10)
        _log("API reloaded")
    except Exception:
        _log("API reload skipped (not running?)")


def run_loop(interval: int) -> None:
    while True:
        try:
            run_once()
        except Exception as e:
            _log(f"cycle error: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        iv = int(sys.argv[sys.argv.index("--interval") + 1]) if "--interval" in sys.argv else 600
        run_loop(iv)
    else:
        run_once()
