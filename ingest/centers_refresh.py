"""Demand-driven center refresh — the AGENT re-reads the source posting, not people.

A center's freshness comes from re-fetching the SOURCE PAGE it was extracted from
(e.g. centrosacopioterremotovenezuela.pages.dev), re-running the Gemma extractor,
and updating every center on that page + ingesting any new ones. Same model as the
person federation (federated.check) — but the unit is the SOURCE PAGE: one fetch
refreshes all its centers, because 125 of ours share one directory page. Per-center
refresh would hit that same page 125 times; this hits it once.

Triggered by a real user's center /ask (capped geocode so the query stays fast) and
by the maintainer loop (full geocode in the background). Never a manual human POST.

Freshness semantics: `last_confirmed` = when the agent last re-read the source and
still saw this center. A center dropped from the page is NOT deleted — its
last_confirmed just stops advancing, so its freshness label naturally decays to
"sin confirmar hace Nd". No guessing "closed" from an extraction miss.
"""
from __future__ import annotations

import json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "pipeline"))
import store_db as db          # noqa: E402
import centers_enrich as ce    # noqa: E402
from extract_web import extract_web, fetch_markdown  # noqa: E402

REFRESH_TTL_MIN = int(os.environ.get("CENTER_REFRESH_TTL_MIN", "180"))  # re-read a source page at most this often
_NS = "centers"   # throttle namespace in the `checked` table (identity = source url)


def _ckey(name: str, municipality: str) -> str:
    return (name + "|" + municipality).lower().strip()


def _fetch(url: str) -> str:
    """Render with firecrawl first (handles JS pages); fall back to a plain GET."""
    try:
        md = subprocess.run(["firecrawl", "scrape", url, "-o", "/dev/stdout"],
                            capture_output=True, text=True, timeout=90).stdout
        if md and md.strip():
            return md
    except Exception:
        pass
    try:
        return fetch_markdown(url)
    except Exception:
        return ""


def refresh_source(conn, url: str, source: str = "", *, force: bool = False,
                   do_geocode: bool = True, max_new_geocode: int | None = None) -> dict:
    """Re-read ONE source page; update its centers + ingest new ones. TTL-throttled.

    max_new_geocode caps how many brand-new centers we geocode synchronously (each is
    a ~1s network call). At query time pass a small cap so the user isn't blocked; the
    maintainer pass (max_new_geocode=None) geocodes the rest later.
    """
    db.ensure_center_columns(conn)
    if not force and db.recently_checked(conn, _NS, url, REFRESH_TTL_MIN):
        return {"url": url, "throttled": True}

    md = _fetch(url)
    if not md.strip():
        db.mark_checked(conn, _NS, url); conn.commit()
        return {"url": url, "fetched": 0, "error": "empty_page"}

    centers = extract_web(md, source=source or url, url=url).get("centers", [])
    updated = added = geocoded = 0
    for c in centers:
        ckey = _ckey(c.get("name", ""), c.get("municipality", ""))
        if not ckey.strip("|"):
            continue
        existed = conn.execute("SELECT center_id, lat FROM centers WHERE ckey=?", (ckey,)).fetchone()
        db.add_center(conn, c)   # upsert page content (needs/address/hours/phone)
        row = conn.execute("SELECT center_id, lat FROM centers WHERE ckey=?", (ckey,)).fetchone()
        cid = row["center_id"]

        # Agent re-read this center on the source just now -> refresh freshness + tags.
        tags = ce.normalize_needs(c.get("needs", "") or "")
        ctype = ce.classify_ctype(c.get("name", "") or "", c.get("needs", "") or "", c.get("ctype", "") or "")
        conn.execute("UPDATE centers SET needs_tags=?, ctype=?, status='active', last_confirmed=? WHERE center_id=?",
                     (json.dumps(tags), ctype, db._now(), cid))

        if do_geocode and row["lat"] is None and (max_new_geocode is None or geocoded < max_new_geocode):
            lat, lng = ce.geocode_center(c.get("address", "") or "", c.get("municipality", "") or "")
            if lat is not None:
                conn.execute("UPDATE centers SET lat=?, lng=? WHERE center_id=?", (lat, lng, cid))
                geocoded += 1
            time.sleep(1.1)   # respect OSM 1 req/s

        conn.commit()   # persist THIS center before the next (slow) call — crash-safe, resumable
        added += existed is None
        updated += existed is not None

    db.mark_checked(conn, _NS, url)
    conn.commit()
    return {"url": url, "extracted": len(centers), "updated": updated, "added": added, "geocoded": geocoded}


def refresh_for_query(conn, *, force: bool = False, max_new_geocode: int | None = 6) -> list[dict]:
    """Demand trigger: re-read every distinct source page behind our centers.

    With ~2 source pages + the TTL throttle, the first center query in each TTL window
    re-reads the pages; the rest are no-ops. Scales by filtering these source rows to
    the query's municipality once there are many pages.
    """
    rows = conn.execute(
        "SELECT DISTINCT url, source FROM centers WHERE url IS NOT NULL AND url<>''").fetchall()
    out = []
    for r in rows:
        try:
            out.append(refresh_source(conn, r["url"], r["source"] or "",
                                      force=force, max_new_geocode=max_new_geocode))
        except Exception as e:
            out.append({"url": r["url"], "error": str(e)})
    return out


if __name__ == "__main__":
    conn = db.connect()
    print(json.dumps(refresh_for_query(conn, force="--force" in sys.argv, max_new_geocode=None), indent=2))
