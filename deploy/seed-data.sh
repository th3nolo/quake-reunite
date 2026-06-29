#!/usr/bin/env bash
# Seed the persistent data volume with the resolved registry DB.
#
# Run ONCE per environment (locally or on the VPS) BEFORE first start, so the API and
# maintainer come up with the full index instead of an empty DB. The DB is PII +
# gitignored and is NEVER baked into the image — it is placed onto the volume here,
# out-of-band. After this, the maintainer keeps it fresh (VR delta + center refresh).
#
#   deploy/seed-data.sh [path/to/directorio.db] [volume_name]
# defaults: out/directorio.db  directorio_data
#
# Uses docker or podman, whichever is on PATH (Dokploy on the VPS = docker).
set -euo pipefail

DB="${1:-out/directorio.db}"
VOL="${2:-directorio_data}"

ENGINE="$(command -v docker || command -v podman || true)"
[ -n "$ENGINE" ] || { echo "ERROR: need docker or podman on PATH"; exit 1; }
[ -f "$DB" ]     || { echo "ERROR: DB not found: $DB"; exit 1; }

echo "engine: $ENGINE"
"$ENGINE" volume inspect "$VOL" >/dev/null 2>&1 || { echo "creating volume $VOL"; "$ENGINE" volume create "$VOL"; }

# copy the DB into the named volume via a throwaway container (works rootless)
"$ENGINE" run --rm \
  -v "$VOL":/data \
  -v "$(realpath "$DB")":/seed.db:ro \
  python:3.12-slim sh -c 'cp /seed.db /data/directorio.db && ls -la /data/directorio.db'

echo "OK: seeded '$DB' -> volume '$VOL' (/data/directorio.db)"
