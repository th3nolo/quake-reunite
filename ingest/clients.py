"""Thin API clients for the ingest pipeline.

  MistralOCR  : document/image/PDF  -> faithful markdown   (pixels -> text)
  Gemma       : text or image       -> structured JSON      (text -> records, + vision)

Keys come from env: OCR_API_KEY (+OCR_API_URL/OCR_MODEL) and CEREBRAS_API_KEY,
or CEREBRAS_API_KEYS (comma-separated) for multi-key rotation + 429 failover.
urllib only, retry/backoff on 429/5xx. OCR call pattern adapted from
github.com/th3nolo/ocr-docs (ocr_service.call_advanced_ocr).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE from the project .env so local runs work without exporting.
    Real environment variables WIN (Docker/Dokploy inject them) — we only fill gaps."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

OCR_URL = os.environ.get("OCR_API_URL", "https://api.mistral.ai/v1/ocr")
OCR_MODEL = os.environ.get("OCR_MODEL", "mistral-ocr-latest")
CEREBRAS_URL = os.environ.get("CEREBRAS_API_URL", "https://api.cerebras.ai/v1/chat/completions")
GEMMA_MODEL = os.environ.get("CEREBRAS_MODEL", "gemma-4-31b")
UA = "directorio-ingest/1.0 (Mozilla/5.0)"

IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".tif": "image/tiff", ".tiff": "image/tiff"}


def _post(url: str, body: dict, key: str, timeout: int = 120) -> dict:
    data = json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA}
    backoff = 2.0
    for attempt in range(1, 5):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            retryable = e.code in {429, 500, 502, 503, 504}
            if not retryable or attempt == 4:
                raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}") from e
        except Exception:
            if attempt == 4:
                raise
        time.sleep(backoff)
        backoff *= 2
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------- Cerebras key pool
def _est_tokens(content, max_tokens: int) -> int:
    """Estimate request tokens. For vision: text/4 + ~1100 tok/image — NOT base64
    byte length (that bug throttled vision to ~1 call/min)."""
    if isinstance(content, list):
        txt = sum(len(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
        n_img = sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        est = txt // 4 + n_img * 1100 + max_tokens
    else:
        est = len(str(content)) // 4 + max_tokens
    return min(est, 100_000)


def _parse_cerebras_keys() -> list[str]:
    multi = os.environ.get("CEREBRAS_API_KEYS", "").strip()
    if multi:
        return [k.strip() for k in multi.split(",") if k.strip()]
    one = os.environ.get("CEREBRAS_API_KEY", "").strip()
    return [one] if one else []


class _KeyPool:
    """Round-robins Cerebras keys, each with its own TokenBucket; on HTTP 429 a key
    cools down and traffic shifts to the others. Two keys ~= 2x throughput."""
    def __init__(self, keys: list[str]):
        from ratelimit import TokenBucket
        self.keys = list(keys)
        self.buckets = {k: TokenBucket() for k in self.keys}
        self.cool = {k: 0.0 for k in self.keys}
        self.rr = 0
        self.lock = threading.Lock()

    def acquire(self, est: int) -> str:
        if not self.keys:
            raise RuntimeError("no Cerebras key (set CEREBRAS_API_KEY or CEREBRAS_API_KEYS)")
        while True:
            with self.lock:
                now = time.monotonic()
                avail = [k for k in self.keys if self.cool[k] <= now]
                if avail:
                    key = avail[self.rr % len(avail)]; self.rr += 1
                    bucket = self.buckets[key]
                else:
                    wait = min(self.cool.values()) - now; key = None
            if key is None:
                time.sleep(max(0.2, min(wait, 5.0))); continue
            bucket.acquire(est)
            return key

    def trip(self, key: str, seconds: float) -> None:
        with self.lock:
            self.cool[key] = time.monotonic() + seconds


_POOL: "_KeyPool | None" = None


def _pool() -> "_KeyPool":
    global _POOL
    keys = _parse_cerebras_keys()
    if _POOL is None or set(_POOL.keys) != set(keys):
        _POOL = _KeyPool(keys)
    return _POOL


def _cerebras_send(body: dict, est: int) -> dict:
    pool = _pool()
    last = "?"
    for _ in range(max(4, 2 * len(pool.keys) + 2)):
        key = pool.acquire(est)
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA}
        try:
            req = urllib.request.Request(CEREBRAS_URL, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code == 429:                       # this key is rate-limited -> cool it, try another
                ra = (e.headers.get("Retry-After") if e.headers else None) or ""
                pool.trip(key, float(ra) if ra.isdigit() else 20.0)
                last = f"429 key…{key[-6:]}"; continue
            if e.code in (500, 502, 503, 504):
                last = str(e.code); time.sleep(1.5); continue
            raise RuntimeError(f"HTTP {e.code}: {txt[:200]}")
        except Exception as ex:
            last = str(ex); time.sleep(1.5); continue
    raise RuntimeError(f"Cerebras failed after key rotation: {last}")


# ---------------------------------------------------------------- Mistral OCR
def ocr_document(path: str | Path, key: str | None = None) -> str:
    """Return faithful markdown for a PDF or image. One string (pages joined)."""
    key = key or os.environ["OCR_API_KEY"]
    path = Path(path)
    ext = path.suffix.lower()
    raw = path.read_bytes()
    if ext == ".pdf":
        kind, mime = "document_url", "application/pdf"
    elif ext in IMAGE_MIME:
        kind, mime = "image_url", IMAGE_MIME[ext]
    else:
        raise ValueError(f"unsupported for OCR: {ext}")
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    body = {"model": OCR_MODEL, "document": {"type": kind, kind: data_url},
            "table_format": "markdown", "include_image_base64": False}
    resp = _post(OCR_URL, body, key, timeout=180)
    pages = resp.get("pages", [])
    return "\n\n".join((p.get("markdown") or "").strip() for p in pages).strip()


# ---------------------------------------------------------------- Gemma (Cerebras)
def gemma_chat(prompt: str, image_path: str | Path | None = None,
               key: str | None = None, max_tokens: int = 6000, temperature: float = 0.0) -> str:
    """Text (and optional single image) -> model reply string. key=None uses the pool."""
    content: list | str
    if image_path:
        p = Path(image_path)
        b64 = base64.b64encode(p.read_bytes()).decode()
        mime = IMAGE_MIME.get(p.suffix.lower(), "image/jpeg")
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]
    else:
        content = prompt
    body = {"model": GEMMA_MODEL, "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens, "temperature": temperature}
    est = _est_tokens(content, max_tokens)
    if key:                       # explicit single-key override -> legacy path
        try:
            from ratelimit import GLOBAL
            GLOBAL.acquire(est)
        except Exception:
            pass
        resp = _post(CEREBRAS_URL, body, key)
    else:                         # pooled: per-key bucket + 429 failover across keys
        resp = _cerebras_send(body, est)
    choices = resp.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or msg.get("reasoning") or ""


def gemma_messages(messages: list[dict], max_tokens: int = 1500, temperature: float = 0.0) -> str:
    """Multi-turn chat (for the agent loop): full messages array -> reply string.
    Goes through the same key pool + rate limiter as gemma_chat."""
    body = {"model": GEMMA_MODEL, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    est = sum(len(str(m.get("content", ""))) // 4 for m in messages) + max_tokens
    resp = _cerebras_send(body, min(est, 100_000))
    choices = resp.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return msg.get("content") or msg.get("reasoning") or ""


def gemma_json(prompt: str, **kw) -> object:
    """gemma_chat + tolerant JSON parse (strips ``` fences)."""
    txt = gemma_chat(prompt, **kw).strip()
    if txt.startswith("```"):
        txt = txt.split("```", 2)[1] if txt.count("```") >= 2 else txt.strip("`")
        if txt.lstrip().startswith("json"):
            txt = txt.lstrip()[4:]
    txt = txt.strip().strip("`").strip()
    start = min([i for i in (txt.find("["), txt.find("{")) if i >= 0], default=0)
    return json.loads(txt[start:])


def gemma_jsonl(prompt: str, **kw) -> list[dict]:
    """For long lists: model returns one JSON object per line (JSONL).

    Parses line-by-line and keeps every COMPLETE object, so a response truncated
    at max_tokens still yields all rows before the cut (no all-or-nothing failure).
    """
    txt = gemma_chat(prompt, **kw)
    rows: list[dict] = []
    for line in txt.splitlines():
        line = line.strip().rstrip(",").strip("`").strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows
