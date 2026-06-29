"""Generate the deliverables from clustered people:
  out/people.json     clustered people (for the search page + downstream use)
  out/records.csv     every raw record, normalized + cluster id (the 'normalized format')
  out/revision.md     review queues: CI conflicts, deceased, name-only merges
  out/buscador.html   self-contained offline search page (data embedded)
"""
from __future__ import annotations

import csv
import html
import json
from difflib import SequenceMatcher
from pathlib import Path

from parse_text import parse_all_text
from parse_photos import parse_photos
from resolve import _ci_relation, cluster

OUT = Path(__file__).resolve().parent.parent / "out"
OUT.mkdir(exist_ok=True)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def add_possible_same(people: list[dict]) -> None:
    """Annotate near-duplicate people that were NOT auto-merged, so a human can
    confirm (e.g. PEREZ MARIANA vs MARINA). Does not merge."""
    for i, p in enumerate(people):
        p["_idx"] = i
        p["possible_same"] = []
    # Block by EVERY name-token prefix and by CI prefix, so near-spellings
    # (MARIANA/MARINA) and near-CIs still land in a common block.
    blocks: dict[str, list[dict]] = {}
    for p in people:
        for tok in set(p["display_name_key"].split()):
            if len(tok) >= 3:
                blocks.setdefault("N" + tok[:3], []).append(p)
        for ci in p["all_ci"]:
            if len(ci) >= 5:
                blocks.setdefault("C" + ci[:4], []).append(p)
    compared: set[tuple[int, int]] = set()
    for raw_group in blocks.values():
        group = list({p["_idx"]: p for p in raw_group}.values())
        for x in range(len(group)):
            for y in range(x + 1, len(group)):
                a, b = group[x], group[y]
                pair = (a["_idx"], b["_idx"]) if a["_idx"] < b["_idx"] else (b["_idx"], a["_idx"])
                if pair in compared:
                    continue
                compared.add(pair)
                na = " ".join(sorted(a["display_name_key"].split()))
                nb = " ".join(sorted(b["display_name_key"].split()))
                r = _ratio(na, nb)
                shared_name_tokens = set(a["display_name_key"].split()) & set(b["display_name_key"].split())
                # ages close?
                aa = a["age"] if a["age"].isdigit() else None
                ab = b["age"] if b["age"].isdigit() else None
                age_close = (aa is None or ab is None or abs(int(aa) - int(ab)) <= 2)
                # ci close?
                ci_close = False
                ci_equal = False
                for ca in a["all_ci"]:
                    for cb in b["all_ci"]:
                        rel = _ci_relation(ca, cb)
                        if rel == "equal":
                            ci_equal = True
                        if rel in {"equal", "flip"}:
                            ci_close = True
                ci_name_supported = bool(shared_name_tokens) or r >= 0.5
                if (r >= 0.82 and age_close) or (ci_equal and age_close) or (ci_close and ci_name_supported and age_close):
                    a["possible_same"].append(b["_idx"])
                    b["possible_same"].append(a["_idx"])


def assign_record_ids(records: list[dict]) -> None:
    """Ensure every extracted source row has a stable audit ID."""
    for idx, record in enumerate(records, start=1):
        record["record_id"] = f"R{idx:05d}"


def assign_person_ids(people: list[dict], records: list[dict]) -> None:
    """Add stable IDs to resolved people and backfill them into source rows."""
    record_to_person: dict[str, str] = {}
    for idx, person in enumerate(people, start=1):
        person_id = f"P{idx:05d}"
        person["person_id"] = person_id
        for appearance in person["appearances"]:
            appearance["person_id"] = person_id
            record_id = appearance.get("record_id")
            if record_id:
                record_to_person[record_id] = person_id
    for record in records:
        record["person_id"] = record_to_person.get(record.get("record_id", ""), "")


def _possible_same_pairs(people: list[dict]) -> list[tuple[dict, dict]]:
    pairs: list[tuple[dict, dict]] = []
    seen: set[tuple[int, int]] = set()
    for i, person in enumerate(people):
        for j in person.get("possible_same", []):
            key = (i, j) if i < j else (j, i)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((people[key[0]], people[key[1]]))
    return sorted(pairs, key=lambda pair: (pair[0]["display_name_key"], pair[1]["display_name_key"]))


def _has_different_ci(a: dict, b: dict) -> bool:
    cis_a = set(a.get("all_ci", []))
    cis_b = set(b.get("all_ci", []))
    return bool(cis_a and cis_b and not (cis_a & cis_b))


def _ci_relation_summary(a: dict, b: dict) -> str:
    cis_a = a.get("all_ci", [])
    cis_b = b.get("all_ci", [])
    if not cis_a or not cis_b:
        return "CI faltante en una lista"
    relations = {_ci_relation(ca, cb) for ca in cis_a for cb in cis_b}
    if "equal" in relations:
        return "CI compartida"
    if "flip" in relations:
        return "CI probablemente transcrita con error"
    return "CI diferente"


def _person_line(person: dict) -> str:
    ci = " / ".join(person.get("all_ci", [])) or "—"
    age = person.get("age") or "?"
    hospitals = ", ".join(person.get("hospitals", [])) or "—"
    return f"{person.get('person_id', 'P?????')} {person['display_name']} (CI {ci}, edad {age}) — {hospitals}"


def write_people_json(people: list[dict]) -> None:
    (OUT / "people.json").write_text(
        json.dumps(people, ensure_ascii=False, indent=1), encoding="utf-8")


def write_records_csv(records: list[dict]) -> None:
    cols = ["record_id", "person_id", "source", "source_type", "hospital",
            "apellidos", "nombres", "full_name", "name_key", "ci", "age",
            "age_unit", "sex", "origin", "status", "obs", "date", "row_raw"]
    with (OUT / "records.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow(r)


def write_revision(people: list[dict], records: list[dict]) -> None:
    multi = [p for p in people if p["in_multiple_hospitals"]]
    ci_conf = [p for p in people if p["ci_conflict"]]
    deceased = [p for p in people if p["deceased"]]
    name_only = [p for p in people if p["only_name_merge"] and p["n_records"] > 1]
    cross = [p for p in people if len({a["source"] for a in p["appearances"]}) > 1]
    possible_pairs = _possible_same_pairs(people)
    possible_ci_pairs = [(a, b) for a, b in possible_pairs if _has_different_ci(a, b)]
    lines = ["# Revisión — Registro SISMO 2026 VZLA", ""]
    lines.append(f"- Registros crudos: **{len(records)}**")
    lines.append(f"- Personas únicas: **{len(people)}**")
    lines.append(f"- En más de una lista (fuentes): **{len(cross)}**")
    lines.append(f"- En más de un hospital (traslados): **{len(multi)}**")
    lines.append(f"- Cédulas en conflicto (dígitos cambiados): **{len(ci_conf)}**")
    lines.append(f"- Posibles mismas personas no fusionadas: **{len(possible_pairs)}**")
    lines.append(f"- Posibles mismas personas con CI diferente: **{len(possible_ci_pairs)}**")
    lines.append(f"- Fallecidos marcados: **{len(deceased)}**")
    lines.append(f"- Fusiones solo por nombre (sin cédula, revisar): **{len(name_only)}**")
    lines.append("")
    lines.append("## Cédulas en conflicto dentro de una persona resuelta")
    lines.append("")
    lines.append("Estas fusiones se hicieron solo cuando la CI parece un error pequeño de transcripción y el nombre también coincide.")
    for p in ci_conf:
        lines.append(f"- {_person_line(p)}")
    lines.append("")
    lines.append("## Posibles mismas personas con CI diferente")
    lines.append("")
    lines.append("Estas NO se fusionaron automáticamente. Revisar con fuente primaria antes de tratarlas como una sola persona.")
    for a, b in possible_ci_pairs:
        name_a = " ".join(sorted(a["display_name_key"].split()))
        name_b = " ".join(sorted(b["display_name_key"].split()))
        score = round(_ratio(name_a, name_b), 3)
        lines.append(f"- {_person_line(a)}")
        lines.append(f"  - Posible par: {_person_line(b)}")
        lines.append(f"  - Motivo: {_ci_relation_summary(a, b)}; similitud de nombre {score}")
    lines.append("")
    lines.append("## Otros posibles duplicados no fusionados")
    lines.append("")
    for a, b in possible_pairs:
        if _has_different_ci(a, b):
            continue
        name_a = " ".join(sorted(a["display_name_key"].split()))
        name_b = " ".join(sorted(b["display_name_key"].split()))
        score = round(_ratio(name_a, name_b), 3)
        lines.append(f"- {_person_line(a)}")
        lines.append(f"  - Posible par: {_person_line(b)}")
        lines.append(f"  - Motivo: {_ci_relation_summary(a, b)}; similitud de nombre {score}")
    lines.append("")
    lines.append("## En más de un hospital")
    for p in multi:
        lines.append(f"- {_person_line(p)}")
    lines.append("")
    lines.append("## Fallecidos")
    for p in deceased:
        lines.append(f"- {_person_line(p)}")
    (OUT / "revision.md").write_text("\n".join(lines), encoding="utf-8")


def write_html(people: list[dict], records: list[dict]) -> None:
    # compact records for the page: drop internal fields
    slim = []
    for p in people:
        slim.append({
            "id": p["person_id"],
            "n": p["display_name"],
            "k": p["display_name_key"],
            "ci": p["all_ci"],
            "age": p["age"],
            "sex": p["sex"],
            "org": p["origins"],
            "hosp": p["hospitals"],
            "st": p["statuses"],
            "dec": p["deceased"],
            "multi": p["in_multiple_hospitals"],
            "cic": p["ci_conflict"],
            "ps": p["possible_same"],
            "ap": [{"h": a["hospital"], "s": a["source"], "st": a["status"],
                    "ci": a["ci"], "age": a["age"], "org": a["origin"],
                    "obs": a["obs"], "date": a["date"]} for a in p["appearances"]],
        })
    data_json = json.dumps(slim, ensure_ascii=False)
    n_people = len(people)
    n_multi = sum(1 for p in people if p["in_multiple_hospitals"])
    n_records = len(records)
    hospitals = sorted({h for p in people for h in p["hospitals"]})
    page = HTML_TEMPLATE
    page = page.replace("/*DATA*/", "const PEOPLE = " + data_json + ";")
    page = page.replace("{{N_PEOPLE}}", str(n_people))
    page = page.replace("{{N_RECORDS}}", str(n_records))
    page = page.replace("{{N_MULTI}}", str(n_multi))
    page = page.replace("{{N_HOSP}}", str(len(hospitals)))
    (OUT / "buscador.html").write_text(page, encoding="utf-8")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Buscador de personas — Sismo Venezuela 2026</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#272b35;--txt:#e8eaed;--mut:#9aa0aa;--accent:#3b82f6;--ok:#22c55e;--warn:#f59e0b;--dead:#9ca3af;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 16px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--mut);font-size:13px}
.wrap{max-width:760px;margin:0 auto;padding:0 16px}
#q{width:100%;padding:13px 14px;font-size:17px;border-radius:10px;border:1px solid var(--line);background:#11141a;color:var(--txt);margin:12px 0}
#q:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.stats{display:flex;gap:14px;flex-wrap:wrap;color:var(--mut);font-size:12px;margin-bottom:6px}
.stats b{color:var(--txt)}
.hint{color:var(--mut);font-size:12px;margin:2px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:13px 14px;margin:10px 0}
.card.multi{border-color:#3b5bdb}
.card.dead{opacity:.92}
.name{font-size:17px;font-weight:650;letter-spacing:.3px}
.meta{color:var(--mut);font-size:13px;margin-top:2px}
.badges{margin-top:8px;display:flex;gap:6px;flex-wrap:wrap}
.b{font-size:11px;padding:3px 8px;border-radius:999px;border:1px solid var(--line)}
.b.h{background:#16261c;border-color:#1f5132;color:#7ee2a8}
.b.f{background:#241016;border-color:#5b2030;color:#f3a8b8}
.b.m{background:#13203f;border-color:#2a47a0;color:#9db8ff}
.b.c{background:#241d10;border-color:#5b4620;color:#f0c879}
.hosp{margin-top:9px}
.hrow{display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-top:1px dashed var(--line);font-size:13px}
.hrow .hn{font-weight:600}
.hrow .hs{color:var(--mut);font-size:12px}
.ps{margin-top:8px;font-size:12px;color:#f0c879}
.empty{color:var(--mut);text-align:center;padding:40px 0}
.disc{color:var(--mut);font-size:12px;border-top:1px solid var(--line);margin-top:24px;padding:14px 0 40px}
mark{background:#3b3000;color:#ffe08a;padding:0 1px;border-radius:2px}
.count{color:var(--mut);font-size:12px;margin:6px 0}
</style>
</head>
<body>
<header><div class="wrap">
  <h1>Buscador de personas — Sismo Venezuela 2026</h1>
  <div class="sub">Ingresos a hospitales. Busca por nombre, apellido o cédula.</div>
  <input id="q" type="search" autocomplete="off" autofocus placeholder="Ej: Pérez, María, 12345678…">
  <div class="stats">
    <span><b>{{N_PEOPLE}}</b> personas</span>
    <span><b>{{N_RECORDS}}</b> registros</span>
    <span><b>{{N_MULTI}}</b> en varios hospitales</span>
    <span><b>{{N_HOSP}}</b> hospitales</span>
  </div>
</div></header>
<div class="wrap">
  <div class="count" id="count"></div>
  <div id="results"></div>
  <div class="disc">
    Datos recopilados de listas informales de hospitales (PDF, registros y fotos) tras el sismo.
    Pueden tener errores de transcripción. <b>Confirma siempre con el hospital.</b>
    Una persona puede aparecer con la cédula o el nombre ligeramente distintos en cada lista;
    por eso la búsqueda es aproximada.
  </div>
</div>
<script>
/*DATA*/
const SRC_LABELS={consolidado_full:"Lista consolidada",consolidado_earlier:"Lista consolidada (versión previa)",registro:"Registro maestro",huc_report:"Reporte oficial HUC",foto_catia:"Foto — Periférico de Catia",foto_luciani:"Foto — Domingo Luciani",foto_perez:"Foto — Pérez Carreño",foto_vargas:"Foto — Vargas de Caracas",foto_albergue:"Foto — Albergue Campo de Golf"};
function srcLabel(s){if(SRC_LABELS[s])return SRC_LABELS[s];if(s&&s.indexOf("foto")===0)return "Foto de lista";return s;}
function fold(s){return (s||"").normalize("NFD").replace(/[̀-ͯ]/g,"").toUpperCase().replace(/Ñ/g,"N").replace(/[^A-Z0-9 ]/g," ").replace(/\s+/g," ").trim();}
// precompute: name tokens and origin tokens kept SEPARATE so a name search does
// not match an origin by accident (e.g. "laya" must not hit "PLAYA GRANDE").
PEOPLE.forEach(p=>{p._name=fold(p.k).split(" ").filter(Boolean);p._org=fold(p.org.join(" ")).split(" ").filter(Boolean);});
function lev(a,b){if(a===b)return 0;const m=a.length,n=b.length;if(!m)return n;if(!n)return m;let prev=Array.from({length:n+1},(_,i)=>i),cur=new Array(n+1);for(let i=1;i<=m;i++){cur[0]=i;for(let j=1;j<=n;j++){cur[j]=Math.min(prev[j]+1,cur[j-1]+1,prev[j-1]+(a[i-1]===b[j-1]?0:1));}[prev,cur]=[cur,prev];}return prev[n];}
function score(p,qToks,qRaw){
  let nameS=0, ciS=0, orgS=0;
  // cédula match (strong, can stand alone)
  if(/^\d{5,}$/.test(qRaw)){ for(const c of p.ci){ if(c===qRaw)ciS=Math.max(ciS,1000); else if(c.includes(qRaw))ciS=Math.max(ciS,120); else if(c.length===qRaw.length){let d=0;for(let i=0;i<c.length;i++)if(c[i]!==qRaw[i])d++; if(d<=1)ciS=Math.max(ciS,90);} } }
  for(const qt of qToks){
    let best=0;
    for(const t of p._name){
      if(t===qt){best=Math.max(best,100);continue;}
      if(t.startsWith(qt)){best=Math.max(best,70+qt.length);continue;}
      if(qt.startsWith(t)&&t.length>=3){best=Math.max(best,55);continue;}
      if(t.includes(qt)&&qt.length>=4){best=Math.max(best,42);continue;}
      if(qt.length>=4&&t.length>=4){const d=lev(qt,t);if(d<=2)best=Math.max(best,42-d*9-Math.abs(t.length-qt.length)*3);}
    }
    nameS+=best;
    // origin only as a tie-breaker, exact/prefix token only (never substring)
    for(const o of p._org){ if(o===qt||(qt.length>=4&&o.startsWith(qt))){orgS=Math.max(orgS,12);} }
  }
  // require a real name or cédula signal; origin alone never qualifies
  const sig = nameS>=40 || ciS>0;
  return {s:nameS+ciS+(sig?orgS:0), sig};
}
function hl(text,qToks){let out=fold(text);return text;}
function badge(p){
  let b="";
  const st=p.st.join(",");
  // Map the REAL status; positive/current state wins. Never default unknowns to
  // "Ingresado" — most people here are missing (por_localizar), not hospitalized.
  if(p.dec||st.includes("fallecido")) b+='<span class="b f">Fallecido</span>';
  else if(st.includes("localizado")) b+='<span class="b h">Localizado / a salvo</span>';
  else if(st.includes("alta")) b+='<span class="b h">De alta</span>';
  else if(st.includes("ingresado")) b+='<span class="b h">Ingresado (hospital)</span>';
  else if(st.includes("herido")) b+='<span class="b c">Herido</span>';
  else if(st.includes("por_localizar")) b+='<span class="b c">Por localizar</span>';
  else b+='<span class="b">'+(esc(st)||"Estado no confirmado")+'</span>';
  if(p.multi) b+='<span class="b m">En '+p.hosp.length+' hospitales</span>';
  if(p.cic) b+='<span class="b c">Cédula a verificar</span>';
  return b;
}
function card(p){
  const ci=p.ci.length?("CI "+p.ci.join(" / ")):"sin cédula";
  const age=p.age?(p.age+" años"):"";
  const sex=p.sex==="F"?"F":p.sex==="M"?"M":"";
  const meta=[p.id,ci,age,sex,p.org.join(", ")].filter(Boolean).join(" · ");
  let hosp="";
  const seen={};
  p.ap.forEach(a=>{const key=a.h+"|"+a.s;if(seen[key])return;seen[key]=1;
    const extra=[a.date,a.org,a.obs].filter(Boolean).join(" · ");
    hosp+='<div class="hrow"><span class="hn">'+esc(a.h||"—")+'</span><span class="hs">'+esc(srcLabel(a.s))+(extra?(" · "+esc(extra)):"")+'</span></div>';});
  let ps="";
  if(p.ps&&p.ps.length){const names=p.ps.map(i=>PEOPLE[i]&&PEOPLE[i].n).filter(Boolean);if(names.length)ps='<div class="ps">⚠ Posible misma persona: '+names.map(esc).join("; ")+'</div>';}
  return '<div class="card'+(p.multi?" multi":"")+(p.dec?" dead":"")+'"><div class="name">'+esc(p.n)+'</div><div class="meta">'+esc(meta)+'</div><div class="badges">'+badge(p)+'</div><div class="hosp">'+hosp+'</div>'+ps+'</div>';
}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
const R=document.getElementById("results"),C=document.getElementById("count"),Q=document.getElementById("q");
function run(){
  const raw=Q.value.trim(), q=fold(raw);
  if(q.length<2){R.innerHTML="";C.textContent="";return;}
  const qToks=q.split(" ").filter(Boolean), qRaw=raw.replace(/\D/g,"");
  const scored=[];
  for(const p of PEOPLE){const r=score(p,qToks,qRaw);if(r.sig)scored.push([r.s,p]);}
  scored.sort((a,b)=>b[0]-a[0]);
  const top=scored.slice(0,80);
  C.textContent=scored.length?(scored.length+" resultado(s)"):"";
  R.innerHTML=top.length?top.map(x=>card(x[1])).join(""):'<div class="empty">Sin resultados para “'+esc(raw)+'”.<br>Prueba con menos letras o solo el apellido.</div>';
}
let t;Q.addEventListener("input",()=>{clearTimeout(t);t=setTimeout(run,90);});
run();
</script>
</body>
</html>"""


def main():
    records = parse_all_text() + parse_photos()
    assign_record_ids(records)
    people = cluster(records)
    assign_person_ids(people, records)
    add_possible_same(people)
    write_people_json(people)
    write_records_csv(records)
    write_revision(people, records)
    write_html(people, records)
    print(f"records={len(records)} people={len(people)} "
          f"multi={sum(p['in_multiple_hospitals'] for p in people)} "
          f"-> out/people.json out/records.csv out/revision.md out/buscador.html")


if __name__ == "__main__":
    main()
