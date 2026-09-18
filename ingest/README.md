# Directorio Sismo 2026 — ingestion + maintainer + API

Turns scattered earthquake sources into one deduped registry, kept fresh by an
autonomous maintainer, served over a REST API. Built on the existing
`pipeline/` (normalize + resolve).

## Data flow
```
SOURCES (sources.yaml)
  pdf/docx ─ pipeline.parse_text
  photos   ─ ingest/extract_docs.py   Mistral OCR → Gemma-4 structure
  web      ─ ingest/extract_web.py    page md → Gemma-4 (persons + centers)
  video    ─ ingest/extract_video.py  yt-dlp → ffmpeg frames → Mistral OCR + Whisper → Gemma-4
        │
        ▼  records (directorio schema)
  ingest/aggregate.py → pipeline.resolve.cluster  (STRONG-ID-only auto-merge)
        │
        ▼
  out/central_people.json · central_records.csv · central_centers.csv · central_review.md
        │
        ▼
  api/app.py (FastAPI)   ←  maintainer/loop.py drives refresh + POST /admin/reload
```

## Models (both verified live on real data)
- **Cerebras `gemma-4-31b`** — text + image. Structuring, agent reasoning, video-frame vision. Limits: 100 req/min, 100k tok/min, 131k ctx (self-throttled in `ingest/ratelimit.py`).
- **Mistral `mistral-ocr-latest`** — document/photo/PDF → markdown. Beats Gemma-vision-alone on dense lists (recovers cédulas).

## Safety rules (enforced)
- **Auto-merge only on strong ID** (cédula; phone/photo when present). Name matches → review queue, never auto-applied. The **common-name guard** in `resolve.py` refuses to merge cédula-less common names ("José Rodríguez") — they would otherwise falsely mark a person "found". (Effect: records.csv 1,678 → 1,788 people; +110 false merges prevented.)
- Every person keeps `appearances[]` (full provenance). `central_review.md` lists CI-conflicts, deceased, name-only merges, possible-same pairs.

## Run locally
```bash
pip install -r requirements.txt
export CEREBRAS_API_KEY=csk-...  OCR_API_KEY=...  DATA_DIR=$PWD/out
python ingest/extract_docs.py            # photos → photos_records.csv
python ingest/extract_web.py             # cached web → web_persons/centers.csv
python ingest/extract_video.py <url> ... # videos → video_persons.csv
python ingest/aggregate.py               # → out/central_*
uvicorn api.app:app --port 8080          # REST API
python maintainer/loop.py --once         # one maintenance cycle (or --loop --interval 600)
```

## REST API
The public `/buscador` HTML embeds the complete exported person index for offline use. Request
throttling and audit logging do not prevent bulk downloads. See the
[public data and offline download model](../README.md#public-data-and-offline-downloads), including
the difference between collecting from search-only sources and publishing the resolved index.

`/health` · `/stats` · `/persons?ci=&name=&status=&municipality=` · `/persons/{ci}` · `/centers?q=&ctype=&municipality=` · `/review` · `POST /admin/reload`. Per-IP rate limit + audit log (`out/api_audit.log`).

## Deploy (Contabo VPS via Dokploy)
`deploy/` has the Dockerfile + `docker-compose.dokploy.yml` (api + maintainer, shared `/data`, Traefik HTTPS). Steps: copy `deploy/.env.example`→`.env`, fill keys (use a **durable** Cerebras key — the hackathon key expires in 24h), set the Traefik `Host(...)` to your domain, deploy as a Compose app on port 8080. `faster-whisper` + `ffmpeg` are in the image for video ASR.

## Gaps / honest notes
- Search-only person registries (hospitalesenvenezuela, desaparecidos, venezuelatebusca…) do NOT bulk-publish people (privacy) → they're cross-check targets, not feeds. Bulk web feeds = acopio centers + SSR registries (venezuelareporta).
- Web extraction can capture UI noise → same review gating applies.
- `sources.yaml` enables 3 web sources; add the rest of `.firecrawl/ve-resources` + video URLs to widen coverage.
