"""Gemma agent that DRIVES the tools (Firecrawl / HTTP / DB), ReAct-style.

This is the production shape the project wants: at runtime the Gemma-4 model — not
Claude Code, not a fixed script — decides to reach for Firecrawl and interacts with
the closed sites, the way we proved manually. Code only *provides* the tools; Gemma
chooses which to call. Works whether or not Cerebras exposes native function-calling
(we prompt a strict one-action-per-turn JSON loop).

Tools Gemma can call:
  db_search        — our local index (fast path)
  http_get         — GET a source's public search API (the GET-able closed sources)
  firecrawl_scrape — render a JS page to read it
  firecrawl_interact — type a query into a JS-only search box (hospitalesve, etc.)
  ingest           — persist+dedupe matches into the index
  finish           — return the grounded answer
"""
from __future__ import annotations

import json, re, subprocess, sqlite3, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("ingest", "pipeline"):
    sys.path.insert(0, str(ROOT / p))
import store_db as db          # noqa: E402
import normalize as nz         # noqa: E402
import federated as fed        # noqa: E402
import cedula as ced           # noqa: E402
import names as nm             # noqa: E402
from clients import gemma_messages  # noqa: E402

UA = "directorio/1.0 (humanitarian; single-person query)"

SYSTEM = """You are the DIRECTORIO agent for the 2026 Venezuela earthquake. You help a family locate ONE person. Work step by step: each turn reply with EXACTLY one JSON object and nothing else:
{"thought":"...","tool":"<tool>","args":{...}}

TOOLS:
- {"tool":"db_search","args":{"ci":"","name":"","status":""}}  -> local index (use FIRST)
- {"tool":"http_get","args":{"url":"..."}}  -> GET a source's public search API (the GET-able closed sources)
- {"tool":"firecrawl_scrape","args":{"url":"..."}}  -> render a JS page
- {"tool":"firecrawl_interact","args":{"url":"...","query":"..."}}  -> type a query into a JS-only search box
- {"tool":"cedula_lookup","args":{"ci":"","nac":"V"}}  -> official name from a cédula (when a source is available)
- {"tool":"derive_from_family","args":{"nombres":"","father":"","mother":""}}  -> derive the child's full surname from the parents (father's apellido + mother's apellido)
- {"tool":"refresh_centers","args":{"url":""}}  -> re-read aid-center source page(s) so status/needs are current (url optional = all). Use for "¿sigue abierto?"/"qué necesitan ahora" center questions.
- {"tool":"ingest","args":{"records":[{"nombre":"","ci":"","estado":"","hospital":"","lugar":""}]}}  -> save matches
- {"tool":"finish","args":{"answer":"..."}}  -> final answer

CLOSED SOURCES (search by NAME; many do NOT store cédula). GET (use http_get):
  encuentralos: https://encuentralos.tecnosoft.dev/api/personas?q={q}
  sosvenezuela2026: https://sosvenezuela2026.com/buscar?q={q}
  radarvzla: https://radarvzla.com/api/buscar?modo=todo&q={q}
JS-only (use firecrawl_interact): hospitalesenvenezuela.com, venezuelatebusca.com, desaparecidosterremotovenezuela.com

RULES:
1) db_search first. 2) If you have a cédula, try cedula_lookup for the official name; if it returns no name and you know relatives, use derive_from_family. 3) Check the closed sources for THIS person only: try the cédula in BOTH formats (e.g. 12345678 and 12.345.678), the name, and its variants (common errors: a dropped or added H, swapped apellidos). 4) If http_get/scrape returns the person's data, call ingest. 5) Then finish. NEVER invent. NEVER enumerate other people — only the person asked about.

LANGUAGE: detect the language of the user's question and write the final `finish` answer in THAT language (Spanish question -> Spanish answer). Cite the source(s) and end with a confirm-with-the-hospital caveat ("Confirme con el hospital/centro; datos comunitarios, no verificados" in Spanish, or its equivalent in the user's language)."""


def _db_search(conn, a) -> str:
    ci = "".join(c for c in str(a.get("ci", "")) if c.isdigit())
    where, args = [], []
    if ci:
        where.append("person_id IN (SELECT person_id FROM person_ci WHERE ci=?)"); args.append(ci)
    if a.get("status"):
        where.append("statuses LIKE ?"); args.append(f'%"{a["status"]}"%')
    for t in nz.name_key("", str(a.get("name", ""))).split():
        where.append("name_key LIKE ?"); args.append(f"%{t}%")
    sql = "SELECT display_name,cis,statuses,origins FROM persons" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY n_records DESC LIMIT 8"
    rows = [{"nombre": r["display_name"], "cedula": db._jget(r, "cis"),
             "estado": db._jget(r, "statuses"), "lugar": db._jget(r, "origins")[:1]}
            for r in conn.execute(sql, args)]
    return json.dumps({"count": len(rows), "rows": rows}, ensure_ascii=False)


def _http_get(a) -> str:
    try:
        req = urllib.request.Request(str(a.get("url", "")), headers={"User-Agent": UA, "Accept": "application/json"})
        return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")[:3500]
    except Exception as e:
        return f"ERROR: {e}"


def _fc_scrape(a) -> str:
    try:
        out = subprocess.run(["firecrawl", "scrape", str(a.get("url", "")), "-o", "/dev/stdout"],
                             capture_output=True, text=True, timeout=75).stdout
        return out[:3500] or "EMPTY"
    except Exception as e:
        return f"ERROR: {e}"


def _fc_interact(a) -> str:
    url, query = str(a.get("url", "")), str(a.get("query", ""))
    try:
        # 1) open a live browser session on the page
        subprocess.run(["firecrawl", "scrape", url, "-o", "/dev/null"], capture_output=True, text=True, timeout=100)
        # 2) drive the JS search box and read the results
        out = subprocess.run(
            ["firecrawl", "interact", "--timeout", "120", "-p",
             f"In the page's search box, search for '{query}'. Return ONLY the matching person rows "
             f"(nombre, cédula, estado, hospital, lugar) as plain text. If no match, reply 'no match'."],
            capture_output=True, text=True, timeout=150).stdout
        return out[:3500] or "EMPTY"
    except Exception as e:
        return f"interact error: {e}; if the site has a GET search use http_get instead"


def _ingest(conn, a) -> str:
    n = 0
    for r in a.get("records", []):
        ap = nz.clean_display(r.get("nombre", ""))
        if not ap:
            continue
        db.add_record(conn, {"source": r.get("source", "agent_federated"), "source_type": "agent",
            "full_name": ap.upper(), "name_key": nz.name_key("", ap), "ci": nz.normalize_ci(str(r.get("ci", ""))),
            "age": str(r.get("edad", "") or ""), "status": fed._norm_status(r.get("estado", "")) or "por_localizar",
            "origin": nz.clean_display(str(r.get("lugar", ""))), "hospital": nz.clean_display(str(r.get("hospital", ""))),
            "obs": f"{r.get('source', 'agent')} (agent)"})
        n += 1
    conn.commit()
    return f"ingested {n} record(s)"


def _refresh_centers(conn, a) -> str:
    """Re-read aid-center source page(s) so status/needs are current (agent, not people)."""
    import centers_refresh
    url = str(a.get("url", "")).strip()
    if url:
        res = centers_refresh.refresh_source(conn, url, force=True, max_new_geocode=6)
    else:
        res = centers_refresh.refresh_for_query(conn, force=True, max_new_geocode=6)
    return json.dumps(res, ensure_ascii=False)


def _parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"tool": "finish", "args": {"answer": raw.strip()[:500]}}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"tool": "finish", "args": {"answer": raw.strip()[:500]}}


def run(question: str, max_steps: int = 10, extra: str = "") -> dict:
    conn = db.connect()
    sysmsg = SYSTEM + (("\n\n" + extra) if extra else "")
    msgs = [{"role": "system", "content": sysmsg}, {"role": "user", "content": question}]
    trace, seen = [], set()
    try:
        for step in range(max_steps):
            raw = gemma_messages(msgs, max_tokens=1200)
            act = _parse(raw)
            tool, args = act.get("tool", "finish"), act.get("args", {})
            trace.append({"step": step, "thought": act.get("thought", ""), "tool": tool, "args": args})
            if tool == "finish":
                return {"question": question, "answer": args.get("answer", ""), "steps": len(trace), "trace": trace}
            def _derive():
                dn = nm.derive_child_name(str(args.get("nombres", "")), str(args.get("father", "")), str(args.get("mother", "")))
                return json.dumps({"derived": dn, "variants": nm.name_variants(dn)}, ensure_ascii=False)
            akey = tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            if akey in seen:                 # don't waste steps repeating an identical call
                obs = "Repeated call (same result as before). Try a different source/variant, or finish."
            else:
                seen.add(akey)
                obs = ({"db_search": lambda: _db_search(conn, args), "http_get": lambda: _http_get(args),
                        "firecrawl_scrape": lambda: _fc_scrape(args), "firecrawl_interact": lambda: _fc_interact(args),
                        "cedula_lookup": lambda: json.dumps(ced.lookup(str(args.get("ci", "")), str(args.get("nac", "V"))), ensure_ascii=False),
                        "derive_from_family": _derive,
                        "refresh_centers": lambda: _refresh_centers(conn, args),
                        "ingest": lambda: _ingest(conn, args)}.get(tool, lambda: f"unknown tool '{tool}'"))()
            msgs.append({"role": "assistant", "content": raw})
            msgs.append({"role": "user", "content": "OBSERVATION:\n" + str(obs)[:3800]})
        return {"question": question, "answer": "(sin respuesta final tras varios pasos)", "steps": len(trace), "trace": trace}
    finally:
        conn.close()


if __name__ == "__main__":
    import sys as _s
    print(json.dumps(run(" ".join(_s.argv[1:]) or "busco a Ana Perez Mora cedula 12345678"),
                     ensure_ascii=False, indent=1))
