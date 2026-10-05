#!/usr/bin/env bash
# Run the semantic-search probe inside the backend container and save a snapshot.
#
#   scripts/search-tuning/run.sh [query-set] [label] [extra probe args...]
#
# CONTAINER=<name> overrides the backend container (default: the compose `backend`
# service of the current directory).
# Extra args go straight to probe.py — e.g. --top-k 25 --budget 40000 for the
# embedding-model A/B, whose snapshots are NOT comparable with default-config ones.
#
# Snapshots land in snapshots/<set>-<label>.json and are diffed with compare.py.
# The probe and the query set are copied in because scripts/ is not mounted into the
# container.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SET="${1:-xling-bench}"
LABEL="${2:-$(date +%Y%m%d-%H%M%S)}"
shift 2 2>/dev/null || true
CONTAINER="${CONTAINER:-$(docker compose ps -q backend)}"
[ -n "$CONTAINER" ] || { echo "backend container not found; set CONTAINER=<name>" >&2; exit 1; }

QUERIES="$HERE/queries/$SET.json"
[ -f "$QUERIES" ] || { echo "no such query set: $QUERIES" >&2; exit 1; }

mkdir -p "$HERE/snapshots"
OUT="$HERE/snapshots/$SET-$LABEL.json"

docker exec "$CONTAINER" mkdir -p /tmp/search-metrics
docker cp "$HERE/probe.py" "$CONTAINER:/tmp/search-metrics/probe.py"
docker cp "$QUERIES" "$CONTAINER:/tmp/search-metrics/queries.json"
docker exec "$CONTAINER" python /tmp/search-metrics/probe.py \
  --queries /tmp/search-metrics/queries.json "$@" > "$OUT"

echo "snapshot: $OUT"
python3 "$HERE/compare.py" --show "$OUT"
