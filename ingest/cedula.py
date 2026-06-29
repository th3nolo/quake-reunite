"""Cédula -> official full name. "When possible": uses whatever source works.

Recon result (2026-06): there is NO anonymous, captcha-free, token-free source
reachable from here:
  * CNE direct (cne.gob.ve)         -> unreachable from this egress
  * sistemaspnp.com, cedula.com.ve web form -> reCAPTCHA (blocks HTTP and Firecrawl)
  * cedula.com.ve API (api.cedula.com.ve) -> live JSON, captcha-FREE, but needs a
    free app_id/app_token (register at cedula.com.ve). The captcha is only on the website.

So: if a token is configured we use the API (clean path); else we try CNE via
Firecrawl as a best-effort; else we return a graceful "unavailable" and the caller
falls back to derive_from_family. Targeted single-cédula use only (no enumeration).
"""
from __future__ import annotations

import json, os, re, subprocess, urllib.parse, urllib.request


def _digits(ci: str) -> str:
    return "".join(c for c in (ci or "") if c.isdigit())


def _name_from_json(d) -> str:
    """Best-effort extraction across known cédula-API JSON shapes."""
    if isinstance(d, dict):
        inner = d.get("data") if isinstance(d.get("data"), dict) else d
        if isinstance(inner, dict):
            parts = [inner.get(k, "") for k in ("primer_nombre", "segundo_nombre", "primer_apellido", "segundo_apellido")]
            joined = " ".join(p for p in parts if p).strip()
            if joined:
                return joined
            for k in ("nombre_completo", "nombre", "fullname", "name"):
                if inner.get(k):
                    return str(inner[k]).strip()
    return ""


def _name_from_cne_html(html: str) -> str:
    m = re.search(r"nombre[^:]*:\s*</?\w*>?\s*([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ\s]{4,})", html, re.I)
    return m.group(1).strip() if m else ""


def lookup(ci: str, nac: str = "V") -> dict:
    d = _digits(ci)
    if not d:
        return {"ok": False, "reason": "no cédula"}

    # 1) configured captcha-free API (set CEDULA_API_URL with {nac}/{ci} placeholders + token)
    base = os.environ.get("CEDULA_API_URL")
    if base:
        try:
            url = base.replace("{nac}", nac).replace("{ci}", d)
            txt = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "directorio/1.0"}),
                                         timeout=15).read().decode("utf-8", "replace")
            name = _name_from_json(json.loads(txt))
            if name:
                return {"ok": True, "nombre": name.upper(), "fuente": "cedula-API"}
        except Exception as e:
            return {"ok": False, "reason": f"API error: {e}"}

    # 2) best-effort via Firecrawl on CNE (often down/blocked, but try when possible)
    try:
        cne = f"http://www.cne.gob.ve/web/registro_electoral/ce.php?nacionalidad={nac}&cedula={d}"
        out = subprocess.run(["firecrawl", "scrape", cne, "-o", "/dev/stdout"],
                             capture_output=True, text=True, timeout=60).stdout
        name = _name_from_cne_html(out)
        if name:
            return {"ok": True, "nombre": name.upper(), "fuente": "CNE (firecrawl)"}
    except Exception:
        pass

    return {"ok": False, "reason": "sin fuente abierta (la API de cedula.com.ve requiere token gratuito; "
                                   "los sitios web tienen captcha). Usa derive_from_family."}


if __name__ == "__main__":
    import sys
    print(json.dumps(lookup(sys.argv[1] if len(sys.argv) > 1 else "12345678"), ensure_ascii=False))
