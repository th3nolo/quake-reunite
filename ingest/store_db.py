"""SQLite backend with streaming, incremental entity resolution.

Why: the CSV path buffered every record in RAM and re-clustered the whole corpus
each run (fragile + unbounded memory). Here each record is INSERTed as it arrives
and resolved against the DB via indexed cédula/name lookups — flat memory,
crash-safe, and the API reads (WAL) while the maintainer writes.

Resolution reuses resolve.py's rules:
  * exact/flip cédula + name  -> merge
  * different cédulas         -> never merge by name
  * name-only                 -> merge ONLY if the name is not corpus-common
                                (the José Rodríguez guard), else new person -> review
Incremental order can differ slightly from batch clustering; it errs toward MORE
persons (safer: a false split beats a false "found").
"""
from __future__ import annotations

import hashlib, json, os, re, sqlite3, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from resolve import _ci_relation  # reuse the cédula typo/flip detector  # noqa: E402

DB_PATH = os.environ.get("DB_PATH", str(Path(os.environ.get("DATA_DIR", ROOT / "out")) / "directorio.db"))
NAME_ONLY = {"name_exact", "name_subset", "name_overlap"}
# match strength — always prefer a cédula match over a name-only one when both exist
_REASON_RANK = {"ci_equal": 4, "ci_flip+name": 3, "name_exact": 2, "name_subset": 2, "name_overlap": 1}

SCHEMA = """
CREATE TABLE IF NOT EXISTS persons(
  person_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name_key TEXT, block TEXT, display_name TEXT, primary_ci TEXT,
  cis TEXT, ages TEXT, sex TEXT, hospitals TEXT, origins TEXT, statuses TEXT,
  deceased INT DEFAULT 0, n_records INT DEFAULT 0,
  only_name_merge INT DEFAULT 0, ci_conflict INT DEFAULT 0, updated_at TEXT);
CREATE INDEX IF NOT EXISTS persons_block ON persons(block);
CREATE INDEX IF NOT EXISTS persons_namekey ON persons(name_key);
CREATE TABLE IF NOT EXISTS records(
  record_id INTEGER PRIMARY KEY AUTOINCREMENT, person_id INT,
  source TEXT, source_uid TEXT, source_type TEXT, full_name TEXT, name_key TEXT, ci TEXT,
  apellidos TEXT, nombres TEXT, age TEXT, age_unit TEXT, sex TEXT, hospital TEXT,
  origin TEXT, status TEXT, obs TEXT, date TEXT, row_raw TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS records_ci ON records(ci);
CREATE INDEX IF NOT EXISTS records_uid ON records(source, source_uid);
CREATE INDEX IF NOT EXISTS records_person ON records(person_id);
CREATE INDEX IF NOT EXISTS records_namekey ON records(name_key);
CREATE TABLE IF NOT EXISTS person_ci(ci TEXT, person_id INT);
CREATE INDEX IF NOT EXISTS person_ci_ci ON person_ci(ci);
CREATE TABLE IF NOT EXISTS tok_df(token TEXT PRIMARY KEY, df INT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS centers(
  center_id INTEGER PRIMARY KEY AUTOINCREMENT, ckey TEXT UNIQUE, name TEXT,
  ctype TEXT, address TEXT, municipality TEXT, hours TEXT, needs TEXT,
  phone TEXT, operator TEXT, source TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS checked(source TEXT, identity TEXT, checked_at TEXT,
  PRIMARY KEY(source, identity));
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _jget(row, field) -> list:
    try:
        return json.loads(row[field] or "[]")
    except Exception:
        return []


def _total(conn) -> int:
    r = conn.execute("SELECT v FROM meta WHERE k='total'").fetchone()
    return int(r[0]) if r else 0


def _df(conn, token) -> int:
    r = conn.execute("SELECT df FROM tok_df WHERE token=?", (token,)).fetchone()
    return r[0] if r else 0


def _age_int(a: str):
    a = (a or "").strip()
    return int(a) if a.isdigit() else None


def _match(rec: dict, person: sqlite3.Row) -> tuple[bool, str]:
    """record vs aggregate person -> (is_same, reason). Mirrors resolve.same_person."""
    ta = set((rec.get("name_key") or "").split())
    tb = set((person["name_key"] or "").split())
    if not ta or not tb:
        return False, ""
    shared = ta & tb
    overlap = len(shared) / max(1, min(len(ta), len(tb)))
    ci = rec.get("ci", "")
    pcis = _jget(person, "cis")
    ci_rel = "none"
    if ci and pcis:
        rels = {_ci_relation(ci, c) for c in pcis}
        ci_rel = "equal" if "equal" in rels else "flip" if "flip" in rels else "diff"
    if ci_rel == "equal" and shared:
        return True, "ci_equal"
    if ci_rel == "flip" and overlap >= 0.5 and shared:
        return True, "ci_flip+name"
    if ci_rel == "diff":
        return False, ""
    # age compatibility (within 1y) if both known
    ra = _age_int(rec.get("age", "")); pa = next((x for x in (_age_int(a) for a in _jget(person, "ages")) if x is not None), None)
    if ra is not None and pa is not None and abs(ra - pa) > 1:
        return False, ""
    if ta == tb:
        return True, "name_exact"
    if shared and (shared == ta or shared == tb) and len(shared) >= 2:
        return True, "name_subset"
    if overlap >= 0.67 and len(shared) >= 2:
        return True, "name_overlap"
    return False, ""


def _is_common(conn, rec: dict, person: sqlite3.Row) -> bool:
    """common-name guard: refuse a name-only merge for corpus-ambiguous names."""
    shared = set((rec.get("name_key") or "").split()) & set((person["name_key"] or "").split())
    if not shared:
        return False
    fmin = max(4, int(0.004 * _total(conn)))
    if len(shared) <= 2 and all(_df(conn, t) >= fmin for t in shared):
        return True
    # same folded name already tied to >=2 distinct cédulas = proven multiple people
    n = conn.execute("SELECT COUNT(DISTINCT ci) FROM records WHERE name_key=? AND ci<>''",
                     (rec.get("name_key", ""),)).fetchone()[0]
    return n >= 2


def _merge_lists(existing: list, *vals) -> list:
    out = list(existing)
    for v in vals:
        if v and v not in out:
            out.append(v)
    return out


def _derive_uid(rec: dict) -> str:
    """Stable id for a source row so re-ingestion writes only the delta.
    Prefer an explicit source id (e.g. VR's obs 'id='); else hash the row content."""
    uid = rec.get("source_uid")
    if uid:
        return str(uid)
    m = re.search(r"id=([^\s|]+)", rec.get("obs", "") or "")
    if m:
        return m.group(1)
    blob = "|".join(str(rec.get(k, "")) for k in ("name_key", "ci", "status", "hospital", "origin", "row_raw"))
    return "h" + hashlib.sha256(blob.encode()).hexdigest()[:22]


def _merge_status(conn, pid: int, status: str) -> None:
    p = conn.execute("SELECT statuses, deceased FROM persons WHERE person_id=?", (pid,)).fetchone()
    if not p:
        return
    stat = _merge_lists(_jget(p, "statuses"), status)
    conn.execute("UPDATE persons SET statuses=?, deceased=?, updated_at=? WHERE person_id=?",
                 (json.dumps(stat), 1 if "fallecido" in stat else p["deceased"], _now(), pid))


def add_record(conn, rec: dict) -> int:
    """Insert one record + resolve it to a person. Idempotent on (source, source_uid):
    a row already seen is skipped (only a status change is applied) -> delta-only re-ingest."""
    src = rec.get("source", "")
    suid = _derive_uid(rec)
    seen = conn.execute("SELECT record_id, person_id, status FROM records WHERE source=? AND source_uid=?",
                        (src, suid)).fetchone()
    if seen:
        if rec.get("status") and rec["status"] != seen["status"]:   # e.g. buscando -> localizado
            conn.execute("UPDATE records SET status=? WHERE record_id=?", (rec["status"], seen["record_id"]))
            _merge_status(conn, seen["person_id"], rec["status"])
        return seen["person_id"]
    nk = rec.get("name_key", "") or ""
    toks = nk.split()
    for t in set(toks):
        conn.execute("INSERT INTO tok_df(token,df) VALUES(?,1) ON CONFLICT(token) DO UPDATE SET df=df+1", (t,))
    conn.execute("INSERT INTO meta(k,v) VALUES('total','1') ON CONFLICT(k) DO UPDATE SET v=CAST(CAST(v AS INT)+1 AS TEXT)")
    ci = rec.get("ci", "") or ""
    block = toks[0][:3] if toks else ""

    cand: set[int] = set()
    if ci:
        for r in conn.execute("SELECT person_id FROM person_ci WHERE ci=?", (ci,)):
            cand.add(r[0])
        for r in conn.execute("SELECT person_id FROM person_ci WHERE ci LIKE ?", (ci[:4] + "%",)):
            cand.add(r[0])
    if block:
        for r in conn.execute("SELECT person_id FROM persons WHERE block=?", (block,)):
            cand.add(r[0])

    pid, best_rank, name_only = None, -1, False
    for c in cand:
        person = conn.execute("SELECT * FROM persons WHERE person_id=?", (c,)).fetchone()
        if not person:
            continue
        ok, reason = _match(rec, person)
        if not ok:
            continue
        if reason in NAME_ONLY and _is_common(conn, rec, person):
            continue
        rank = _REASON_RANK.get(reason, 0)
        if rank > best_rank:                 # pick the STRONGEST match, not the first
            pid, best_rank, name_only = c, rank, reason in NAME_ONLY

    if pid is None:
        pid = _create_person(conn, rec, block)
    else:
        _update_person(conn, pid, rec, name_only)

    if ci and not conn.execute("SELECT 1 FROM person_ci WHERE ci=? AND person_id=?", (ci, pid)).fetchone():
        conn.execute("INSERT INTO person_ci(ci,person_id) VALUES(?,?)", (ci, pid))

    conn.execute("""INSERT INTO records(person_id,source,source_uid,source_type,full_name,name_key,ci,apellidos,
        nombres,age,age_unit,sex,hospital,origin,status,obs,date,row_raw,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, src, suid, rec.get("source_type", ""), rec.get("full_name", ""), nk, ci,
         rec.get("apellidos", ""), rec.get("nombres", ""), rec.get("age", ""), rec.get("age_unit", ""),
         rec.get("sex", ""), rec.get("hospital", ""), rec.get("origin", ""), rec.get("status", ""),
         rec.get("obs", ""), rec.get("date", ""), rec.get("row_raw", ""), _now()))
    return pid


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _create_person(conn, rec: dict, block: str) -> int:
    ci = rec.get("ci", "") or ""
    cur = conn.execute("""INSERT INTO persons(name_key,block,display_name,primary_ci,cis,ages,sex,
        hospitals,origins,statuses,deceased,n_records,only_name_merge,ci_conflict,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("name_key", ""), block, rec.get("full_name", ""), ci,
         json.dumps([ci] if ci else []), json.dumps([rec["age"]] if rec.get("age") else []),
         rec.get("sex", ""), json.dumps([rec["hospital"]] if rec.get("hospital") else []),
         json.dumps([rec["origin"]] if rec.get("origin") else []),
         json.dumps([rec["status"]] if rec.get("status") else []),
         1 if rec.get("status") == "fallecido" else 0, 1, 0, 0, _now()))
    return cur.lastrowid


def _update_person(conn, pid: int, rec: dict, name_only: bool) -> None:
    p = conn.execute("SELECT * FROM persons WHERE person_id=?", (pid,)).fetchone()
    ci = rec.get("ci", "") or ""
    cis = _merge_lists(_jget(p, "cis"), ci)
    ages = _merge_lists(_jget(p, "ages"), rec.get("age", ""))
    hosp = _merge_lists(_jget(p, "hospitals"), rec.get("hospital", ""))
    orig = _merge_lists(_jget(p, "origins"), rec.get("origin", ""))
    stat = _merge_lists(_jget(p, "statuses"), rec.get("status", ""))
    # union the name tokens so future matches see the fuller name
    nk = " ".join(sorted(set((p["name_key"] or "").split()) | set((rec.get("name_key") or "").split())))
    conn.execute("""UPDATE persons SET name_key=?, cis=?, primary_ci=?, ages=?, sex=COALESCE(NULLIF(sex,''),?),
        hospitals=?, origins=?, statuses=?, deceased=?, n_records=n_records+1,
        only_name_merge=MAX(only_name_merge,?), ci_conflict=?, updated_at=?,
        display_name=CASE WHEN length(?)>length(display_name) THEN ? ELSE display_name END
        WHERE person_id=?""",
        (nk, json.dumps(cis), (cis[0] if cis else ""), json.dumps(ages), rec.get("sex", ""),
         json.dumps(hosp), json.dumps(orig), json.dumps(stat),
         1 if ("fallecido" in stat) else p["deceased"],
         1 if name_only else 0, 1 if len([c for c in cis if c]) > 1 else 0, _now(),
         rec.get("full_name", ""), rec.get("full_name", ""), pid))


# ---- centers ----
def add_center(conn, c: dict) -> None:
    key = ((c.get("name", "") + "|" + c.get("municipality", "")).lower().strip())
    conn.execute("""INSERT INTO centers(ckey,name,ctype,address,municipality,hours,needs,phone,operator,source,url)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(ckey) DO UPDATE SET
          address=COALESCE(NULLIF(excluded.address,''),address),
          hours=COALESCE(NULLIF(excluded.hours,''),hours),
          phone=COALESCE(NULLIF(excluded.phone,''),phone),
          needs=COALESCE(NULLIF(excluded.needs,''),needs)""",
        (key, c.get("name", ""), c.get("ctype", ""), c.get("address", ""), c.get("municipality", ""),
         c.get("hours", ""), c.get("needs", ""), c.get("phone", ""), c.get("operator", ""),
         c.get("source", ""), c.get("url", "")))


def ensure_center_columns(conn) -> None:
    """Add the freshness/taxonomy/geo columns to centers (idempotent migration)."""
    for col, typ in (("needs_tags", "TEXT"), ("status", "TEXT"), ("last_confirmed", "TEXT"),
                     ("confirmations", "INTEGER"), ("lat", "REAL"), ("lng", "REAL"),
                     ("canonical_id", "INTEGER"), ("merged_sources", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE centers ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()


def confirm_center(conn, center_id: int, status: str = "", needs: str = "") -> dict | None:
    """Crowd-confirm a center: refresh last_confirmed + status (+ optional new needs)."""
    ensure_center_columns(conn)
    sets, args = ["last_confirmed=?", "confirmations=COALESCE(confirmations,0)+1"], [_now()]
    if status:
        sets.insert(0, "status=?"); args.insert(0, status)
    if needs:
        sets.append("needs=?"); args.append(needs)
    args.append(center_id)
    conn.execute(f"UPDATE centers SET {', '.join(sets)} WHERE center_id=?", args)
    conn.commit()
    r = conn.execute("SELECT center_id,name,status,last_confirmed,confirmations,needs FROM centers WHERE center_id=?",
                     (center_id,)).fetchone()
    return dict(r) if r else None


# ---- queries (for the API) ----
def stats(conn) -> dict:
    g = lambda q, *a: conn.execute(q, a).fetchone()[0]
    by = {r[0] or "?": r[1] for r in conn.execute(
        "SELECT json_each.value, COUNT(*) FROM persons, json_each(persons.statuses) GROUP BY 1")}
    return {"persons": g("SELECT COUNT(*) FROM persons"),
            "records": g("SELECT COUNT(*) FROM records"),
            "with_cedula": g("SELECT COUNT(*) FROM persons WHERE primary_ci<>''"),
            "deceased": g("SELECT COUNT(*) FROM persons WHERE deceased=1"),
            "name_only_merges": g("SELECT COUNT(*) FROM persons WHERE only_name_merge=1 AND n_records>1"),
            "ci_conflicts": g("SELECT COUNT(*) FROM persons WHERE ci_conflict=1"),
            "centers": g("SELECT COUNT(*) FROM centers"),
            "by_status": by}


def record_cycle(conn, changed: bool = True, seconds: float = 0.0, note: str = "") -> dict:
    """Persist a snapshot of the index each maintainer cycle, so we have a durable time-series
    of how the data changes (newly found, newly hospitalized, newly missing) — observability
    that survives container restarts (unlike stdout logs)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS cycle_metrics(
        ts TEXT, persons INT, records INT, with_cedula INT, deceased INT,
        por_localizar INT, localizado INT, ingresado INT, herido INT,
        centers INT, changed INT, seconds REAL, note TEXT)""")
    s = stats(conn); by = s.get("by_status", {})
    conn.execute("INSERT INTO cycle_metrics VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (_now(), s["persons"], s["records"], s["with_cedula"], s["deceased"],
                  by.get("por_localizar", 0), by.get("localizado", 0), by.get("ingresado", 0),
                  by.get("herido", 0), s["centers"], int(bool(changed)), float(seconds), note))
    conn.commit()
    return {"ts": _now(), "persons": s["persons"], "records": s["records"]}


def recently_checked(conn, source: str, identity: str, ttl_min: int) -> bool:
    """True if this (source, identity) was checked within ttl_min — avoids re-hitting
    external sources for the same person (cache; not brute force)."""
    import calendar
    r = conn.execute("SELECT checked_at FROM checked WHERE source=? AND identity=?",
                     (source, identity)).fetchone()
    if not r:
        return False
    try:
        age = time.time() - calendar.timegm(time.strptime(r[0], "%Y-%m-%dT%H:%M:%SZ"))
        return age < ttl_min * 60
    except Exception:
        return False


def mark_checked(conn, source: str, identity: str) -> None:
    conn.execute("""INSERT INTO checked(source,identity,checked_at) VALUES(?,?,?)
        ON CONFLICT(source,identity) DO UPDATE SET checked_at=excluded.checked_at""",
        (source, identity, _now()))


def person_to_dict(p: sqlite3.Row) -> dict:
    return {"person_id": f"P{p['person_id']:05d}", "display_name": p["display_name"],
            "all_ci": _jget(p, "cis"), "ci": p["primary_ci"], "ages": _jget(p, "ages"),
            "sex": p["sex"], "origins": _jget(p, "origins"), "hospitals": _jget(p, "hospitals"),
            "statuses": _jget(p, "statuses"), "deceased": bool(p["deceased"]),
            "n_records": p["n_records"], "ci_conflict": bool(p["ci_conflict"]),
            "only_name_merge": bool(p["only_name_merge"])}
