#!/usr/bin/env bash
# Regenerate the repo-root openapi.json from the current backend code. Run from
# the repo root on a running dev stack (`docker compose up -d backend`). CI runs
# the same export as a drift gate (backend-lint: openapi.json), so a local run
# here is what you do to refresh the committed artifact before committing.
#
# Why a helper: the backend container mounts ./backend:/app (so it writes
# host-side there), and the artifact lives at the repo root — one mv stages it.
set -euo pipefail
cd "$(dirname "$0")/.."

# /app is the backend container's bind mount of ./backend, so the write lands
# host-side at backend/openapi.json.
docker compose exec -T backend python /app/scripts/export_openapi.py --out /app/openapi.json
mv -f backend/openapi.json openapi.json

echo "regenerated openapi.json"
