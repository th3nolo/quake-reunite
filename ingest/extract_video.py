"""Video -> person records.

Pipeline per video URL:
  yt-dlp  download  ->  ffmpeg keyframes  ->  Mistral OCR each frame (on-screen
  printed lists)  +  faster-whisper transcript (spoken names)  ->  Gemma-4
  structures the combined text into records (directorio schema).

Cerebras Gemma-4 takes images but NOT video; audio isn't supported there either,
hence ffmpeg frames + Whisper. faster-whisper is an optional import (install on
the VPS). Frames showing the same list are de-duplicated by OCR-text hash.
"""
from __future__ import annotations

import csv, hashlib, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import normalize as nz  # noqa: E402
from clients import ocr_document, gemma_jsonl  # noqa: E402

VIDEO_PROMPT = """Below is text pulled from a Venezuela 2026-earthquake video: OCR of on-screen frames and/or an audio transcript. Extract every PERSON mentioned as hospitalized / missing / found. Return JSONL, one object per person:
{"apellidos":"","nombres":"","ci":"<digits or ''>","age":"","sex":"M/F/''","status":"ingresado/alta/fallecido/por_localizar/''","hospital":"","origin":"","raw":"<source snippet>"}
Transcribe faithfully, never invent. Skip greetings/commentary. ci = digits only."""


def _run(cmd: list[str], timeout: int = 300) -> int:
    return subprocess.run(cmd, capture_output=True, timeout=timeout).returncode


def download_video(url: str, dest_dir: Path) -> Path | None:
    out = dest_dir / "video.%(ext)s"
    rc = _run(["yt-dlp", "-q", "--no-playlist", "-o", str(out), url], timeout=300)
    if rc != 0:
        return None
    files = [p for p in dest_dir.iterdir() if p.stem == "video"]
    return files[0] if files else None


def keyframes(video: Path, dest_dir: Path, every_sec: float = 3.0) -> list[Path]:
    pat = dest_dir / "f_%04d.jpg"
    _run(["ffmpeg", "-y", "-i", str(video), "-vf", f"fps=1/{every_sec}", "-q:v", "3", str(pat)], timeout=300)
    return sorted(dest_dir.glob("f_*.jpg"))


def transcribe(video: Path) -> str:
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return ""  # not installed locally; runs on the VPS
    try:
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(video), language="es")
        return " ".join(s.text for s in segments)
    except Exception:
        return ""


def _structure(text: str, source: str, url: str, ts: str = "") -> list[dict]:
    if not text.strip():
        return []
    rows = gemma_jsonl(VIDEO_PROMPT + "\n\nTEXT:\n" + text[:60000], max_tokens=16000)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ap = nz.clean_display(r.get("apellidos", "")); no = nz.clean_display(r.get("nombres", ""))
        if not (ap or no):
            continue
        out.append({
            "source": source, "source_type": "video_gemma",
            "hospital": nz.canonical_hospital(str(r.get("hospital", ""))) or nz.clean_display(str(r.get("hospital", ""))),
            "apellidos": ap.upper(), "nombres": no.upper(),
            "full_name": (ap + " " + no).strip().upper(), "name_key": nz.name_key(ap, no),
            "ci": nz.normalize_ci(str(r.get("ci", ""))), "age": str(r.get("age", "")).strip(),
            "age_unit": "", "sex": nz.parse_sex(str(r.get("sex", ""))),
            "origin": nz.clean_display(str(r.get("origin", ""))),
            "status": str(r.get("status", "")).strip().lower(),
            "obs": f"video:{url} {ts}".strip(), "date": "", "row_raw": nz.clean_display(str(r.get("raw", ""))),
        })
    return out


def extract_video_url(url: str, source: str = "video") -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        vid = download_video(url, d)
        if not vid:
            print(f"  ! download failed: {url}")
            return []
        frames = keyframes(vid, d)
        seen, ocr_texts = set(), []
        for fr in frames:
            try:
                txt = ocr_document(fr)
            except Exception:
                continue
            h = hashlib.sha256(txt.encode()).hexdigest()[:12]
            if txt.strip() and h not in seen:   # dedup repeated on-screen list frames
                seen.add(h); ocr_texts.append(txt)
        transcript = transcribe(vid)
        combined = "\n\n".join(ocr_texts)
        if transcript:
            combined += "\n\n[TRANSCRIPT]\n" + transcript
        recs = _structure(combined, source, url)
        print(f"  {url}: {len(frames)} frames, {len(ocr_texts)} unique list-frames, "
              f"{'transcript' if transcript else 'no-asr'} -> {len(recs)} persons")
        return recs


def ingest_urls(urls: list[str], out_csv: str) -> int:
    cols = ["source", "source_type", "hospital", "apellidos", "nombres", "full_name", "name_key",
            "ci", "age", "age_unit", "sex", "origin", "status", "obs", "date", "row_raw"]
    all_rows: list[dict] = []
    for u in urls:
        all_rows += extract_video_url(u)
    if all_rows:
        Path(out_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(all_rows)
    return len(all_rows)


if __name__ == "__main__":
    urls = sys.argv[1:]
    n = ingest_urls(urls, str(ROOT / "ingest" / "out" / "video_persons.csv"))
    print(f"video persons: {n}")
