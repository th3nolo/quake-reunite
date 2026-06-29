"""Natural-language consultation layer (text-to-query, NOT RAG).

Flow per question:
  1. Gemma PLANS a structured query from the NL question (which table + filters).
  2. Code runs it against SQLite (exact/fuzzy, deterministic).
  3. Gemma PHRASES a plain-language answer from ONLY those rows, with source +
     a "confirm with the hospital/center" caveat. No invention, no embeddings.

Designed for low-literacy / low-bandwidth users: text in, text out (works over
WhatsApp/SMS). Same engine answers about people and aid centers.
"""
from __future__ import annotations

import json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in ("ingest", "pipeline"):
    sys.path.insert(0, str(ROOT / p))
import store_db as db          # noqa: E402
import normalize as nz         # noqa: E402  (fold = same accent/case folding as name_key)
from clients import gemma_chat, gemma_json  # noqa: E402

PLANNER = """You convert a question about the 2026 Venezuela earthquake into ONE JSON query.
Tables: "persons" (missing/found/hospitalized people) and "centers" (centros de acopio / refugios).
Output ONLY this JSON (omit empty fields is fine):
{"tool":"persons"|"centers","ci":"","name":"","status":"por_localizar|localizado|ingresado|fallecido|","municipality":"","need":""}
Rules: digits of a cédula -> ci. a person's name -> name. a place/parish -> municipality.
a requested supply (insulina, agua, pañales...) -> need AND tool=centers. "desaparecido"->status por_localizar; "encontrado/a salvo"->localizado; "hospital/ingresado"->ingresado.
Question: """

ANSWERER = """Eres un asistente para familias bajo estrés tras el terremoto de Venezuela 2026, en conexiones lentas. Responde en el MISMO idioma de la pregunta, BREVE, claro y humano. Usa SOLO los registros dados; nunca inventes.
Para CADA persona, UNA línea simple: el nombre, su estado en palabras (por localizar / a salvo / en hospital / fallecida) y dónde (hospital o lugar). NUNCA muestres códigos internos de fuente ni listas de estados crudos.
Si hay varias, lístalas brevemente para que la familia compare. Si 'total' supera lo mostrado, añade: "hay N en total; busca por cédula o apellido para afinar".
Cierra con UNA frase corta: confirmar con el hospital o centro (datos comunitarios). Si no hay registros, dilo y sugiere reportar en venezuelareporta.org.
DATOS: """


def _clean_name(n: str) -> str:
    parts = [p.strip() for p in (n or "").split(" / ") if p.strip()]
    return max(parts, key=len) if parts else (n or "")


def _human_status(sts, deceased=False) -> str:
    sts = sts or []
    if deceased or "fallecido" in sts:
        return "fallecida/o (en revisión)"
    if "localizado" in sts or "alta" in sts:
        return "localizada/o, a salvo"
    if "ingresado" in sts or "herido" in sts:
        return "ingresada/o en un hospital"
    if "por_localizar" in sts:
        return "aún por localizar"
    return "estado no confirmado"


def _persons(conn, q: dict, limit: int = 12) -> tuple[list[dict], int]:
    where, args = [], []
    if q.get("ci"):
        d = "".join(c for c in str(q["ci"]) if c.isdigit())
        if d:
            where.append("person_id IN (SELECT person_id FROM person_ci WHERE ci=?)"); args.append(d)
    if q.get("status"):
        where.append("statuses LIKE ?"); args.append(f'%"{q["status"]}"%')
    if q.get("municipality"):
        where.append("(origins LIKE ? OR hospitals LIKE ?)"); args += [f"%{q['municipality']}%"] * 2
    for tok in nz.fold(str(q.get("name", ""))).split():   # fold so accented input (e.g. "Pérez") matches name_key
        where.append("name_key LIKE ?"); args.append(f"%{tok}%")
    base = "FROM persons" + (" WHERE " + " AND ".join(where) if where else "")
    total = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    # people WITH a cédula first (more identifiable), then most-reported
    rows = conn.execute("SELECT * " + base + " ORDER BY (primary_ci<>'') DESC, n_records DESC LIMIT ?",
                        args + [limit]).fetchall()
    out = []
    for r in rows:
        d = db.person_to_dict(r)   # standard shape: consistent with /persons + the map UI
        d["fuentes"] = conn.execute("SELECT GROUP_CONCAT(DISTINCT source) FROM records WHERE person_id=?",
                                    (r["person_id"],)).fetchone()[0]
        out.append(d)
    return out, total


def _centers(conn, q: dict) -> list[dict]:
    where, args = ["canonical_id IS NULL"], []   # hide centers merged into a canonical one
    if q.get("need"):
        where.append("needs LIKE ?"); args.append(f"%{q['need']}%")
    if q.get("municipality"):
        where.append("(municipality LIKE ? OR address LIKE ?)"); args += [f"%{q['municipality']}%"] * 2
    sql = "SELECT name,address,municipality,hours,needs,phone,status,last_confirmed,lat,lng FROM centers"
    sql += (" WHERE " + " AND ".join(where) if where else "") + " LIMIT 8"
    return [dict(r) for r in conn.execute(sql, args)]


def answer(question: str, conn: sqlite3.Connection | None = None) -> dict:
    close = conn is None
    if conn is None:
        conn = sqlite3.connect(db.DB_PATH); conn.row_factory = sqlite3.Row
    try:
        try:
            plan = gemma_json(PLANNER + question, max_tokens=200)
        except Exception:
            plan = {"tool": "persons", "name": question}
        if not isinstance(plan, dict):
            plan = {"tool": "persons", "name": question}
        checked = None
        if plan.get("tool") == "centers":
            # demand-driven refresh: agent re-reads the source page(s) so status/needs
            # are current, THEN read the local index. Capped geocode keeps it fast.
            try:
                import centers_refresh
                checked = {"centers_refresh": centers_refresh.refresh_for_query(conn, max_new_geocode=6)}
            except Exception as e:
                checked = {"error": str(e)}
            rows = _centers(conn, plan); total = len(rows)
        else:
            # demand-driven federation: for THIS identity, check external sources,
            # ingest results, THEN read the (possibly enriched) local index.
            rows, total = _persons(conn, plan)            # DB first (~17ms)
            # escalate to external sources ONLY when we don't already have the person —
            # found people return instantly; only a genuine miss pays the federation cost.
            if total == 0 and (plan.get("ci") or len(str(plan.get("name", "")).split()) >= 2):
                try:
                    import federated
                    checked = federated.check(conn, ci=str(plan.get("ci", "")), name=str(plan.get("name", "")))
                    rows, total = _persons(conn, plan)    # re-read after any ingest
                except Exception as e:
                    checked = {"error": str(e)}
        # feed Gemma a CLEAN view — no internal source codes, status resolved to plain words
        if plan.get("tool") == "centers":
            clean = [{"nombre": r.get("name"), "municipio": r.get("municipality"),
                      "necesita": r.get("needs"), "estado": r.get("status") or "activo"} for r in rows]
        else:
            clean = [{"nombre": _clean_name(r.get("display_name", "")),
                      "cedula": "/".join(r.get("all_ci") or []) or "sin cédula",
                      "estado": _human_status(r.get("statuses"), r.get("deceased")),
                      "donde": ", ".join(r.get("hospitals") or []) or (r.get("origins") or [""])[0]} for r in rows]
        try:
            reply = gemma_chat(ANSWERER + json.dumps({"pregunta": question, "total": total, "registros": clean},
                                                     ensure_ascii=False), max_tokens=600).strip()
        except Exception as e:   # backend down -> still return the rows, just unphrased
            reply = (f"Encontré {total} resultado(s). No pude redactar la respuesta ahora; "
                     f"revisa la lista. ({type(e).__name__})") if total else "No encontré resultados."
        return {"question": question, "query": plan, "total": total, "shown": len(rows),
                "answer": reply, "results": rows, "checked": checked}
    finally:
        if close:
            conn.close()


if __name__ == "__main__":
    print(json.dumps(answer(" ".join(sys.argv[1:])), ensure_ascii=False, indent=1))
