"""Source registry + maintainer state (cadence + content-hash change detection)."""
from __future__ import annotations
import hashlib, json, os, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "out"))
STATE_FILE = DATA_DIR / "maintainer_state.json"


def load_sources(path: str | None = None) -> list[dict]:
    import yaml
    p = Path(path) if path else ROOT / "ingest" / "sources.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))["sources"]


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def content_hash(src: dict) -> str:
    """Hash the source's current content so we re-ingest only on real change."""
    h = hashlib.sha256()
    kind = src.get("kind")
    if kind in ("web_cache",) and src.get("path") and Path(src["path"]).exists():
        h.update(Path(src["path"]).read_bytes())
    elif kind in ("photos", "pdf") and src.get("path"):
        base = ROOT / src["path"]
        for f in sorted(base.rglob("*")) if base.exists() else []:
            if f.is_file():
                h.update(f.name.encode()); h.update(str(int(f.stat().st_mtime)).encode())
    elif kind == "video":
        h.update(",".join(src.get("urls", [])).encode())
    return h.hexdigest()[:16]


def is_due(src: dict, state: dict, now: float | None = None) -> bool:
    now = now or time.time()
    st = state.get(src["id"], {})
    if content_hash(src) != st.get("hash", ""):
        return True  # content changed -> always due
    last = st.get("last_epoch", 0)
    return (now - last) >= src.get("cadence_min", 360) * 60


def mark_done(src: dict, state: dict) -> None:
    state[src["id"]] = {"hash": content_hash(src), "last_run": now_iso(), "last_epoch": time.time()}
