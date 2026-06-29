# Directorio Sismo 2026 — API + autonomous maintainer
# Build from repo root:  podman build -f deploy/Dockerfile -t directorio .
# (compose uses context: .. so the build context is the repo root either way)
FROM python:3.12-slim

# curl = healthcheck; nodejs+npm = firecrawl-cli (Gemma-driven scraping/interact of
# JS + closed sites: center refresh, cédula CNE, federated sources, agent tools).
# firecrawl-cli needs node>=18; Debian bookworm ships node 18, so no nodesource needed.
# (Video deps ffmpeg/libgomp1 + faster-whisper/yt-dlp were dropped — the `video`
#  source is disabled; re-add them in a derived image to enable it.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl nodejs npm \
    && npm install -g firecrawl-cli@1.19.6 \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code (see .dockerignore — PII/secrets/raw data are excluded)
COPY . .

# non-root + writable data dir
RUN useradd -m -u 10001 app \
    && mkdir -p /data \
    && chown -R app:app /app /data
USER app

ENV DATA_DIR=/data \
    API_RATE_PER_MIN=120 \
    PYTHONUNBUFFERED=1
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

# default = API. The maintainer service overrides `command` in compose.
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8080"]
