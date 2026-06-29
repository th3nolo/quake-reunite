# Directorio Sismo La Guaira 2026

A single humanitarian API + map that **de-duplicates and centralizes** the scattered
reports after the June 24, 2026 La Guaira/Vargas twin earthquake — missing people,
people found in hospitals, and aid collection centers — so a family can search **one
place** instead of a dozen captcha-walled, app-fragmented lists.

Built for the **Cerebras × Google DeepMind Gemma 4 hackathon** — Gemma 4 31B on Cerebras
is the engine for extraction, natural-language search, and agentic source-research.

## What it does
- **De-dup engine** (SQLite): resolves the same person across sources by cédula/name, with
  a common-name guard so two different "José Rodríguez" never merge without strong ID.
- **Multimodal ingest**: Mistral OCR + Gemma turn hospital-list **photos/PDFs** into records.
- **Agentic federation**: a real query escalates to external sources (only on a local miss),
  probed in parallel — the index grows from genuine demand, never enumeration.
- **Aid centers**: needs taxonomy, geocoding, freshness, cross-source dedup.
- **Maps**: a 3D affected-buildings map (MapLibre + Microsoft AI-for-Good footprints + terrain),
  a 2D map, and a self-contained offline search page. People are placed at building/sector
  level (Photon + snap-to-footprint), with directions to hospitals/centers.
- **`/ask`**: natural-language Spanish answers, grounded only in the data.
- **Observability**: `/salud` dashboard + durable per-cycle metrics.

## Stack
Python · FastAPI · SQLite · Gemma 4 31B (Cerebras) · Mistral OCR · Firecrawl · MapLibre GL · OSM/Photon.

## Run
```
pip install -r requirements.txt
DATA_DIR=./out uvicorn api.app:app --host 0.0.0.0 --port 8080   # API + maps
python maintainer/loop.py --loop --interval 600                 # keep the index fresh
```
Deploy (Docker / Dokploy): see `deploy/` (`Dockerfile`, `docker-compose.dokploy.yml`,
`.env.example`, `seed-data.sh`).

## Privacy
This is real disaster data. **No PII and no API keys are in this repository** — the resolved
database, source photos, and scraped material are gitignored and mounted at runtime; example
cédulas/names in the code are fictional. Keys come from environment variables only.
