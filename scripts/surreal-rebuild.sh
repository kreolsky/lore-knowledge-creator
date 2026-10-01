#!/usr/bin/env bash
# SYSTEM: ops-rebuild
# Export→import rebuild of a SurrealKV store (prod lore on CT702; rehearsable on dev
# via env overrides): dump completeness checks, fresh-dir import, mv-aside of the old
# store, table-count parity. The `surreal` CLI is not used (its export was broken on
# v3.1.x/surrealkv; the HTTP routes work on every version): dump = GET /export,
# import = POST /import, both via a curl container sharing surreal's netns (the same
# pattern as the "Backup prod DB" step in .gitea/workflows/deploy.yml).
#
# ORDER IS FIXED (the dump is the new source of truth — no writes may land after it):
#   pre-flight → stop app containers → dump → verify → capture before-counts →
#   stop surreal → mv store aside → fresh store → import → parity → start apps.
#
# Usage:
#   surreal-rebuild.sh --dry-run   dump + verify + before-counts only (hot, NO stop,
#                                  NO import; dump saved as rebuild-dryrun-<TS> —
#                                  never a restore candidate, taken hot)
#   surreal-rebuild.sh --confirm   the real path (downtime: minutes)
#   surreal-rebuild.sh --confirm --resume <dump.surql.gz>
#                                  retry after a failed import: dump phase skipped,
#                                  the given VERIFIED rebuild dump is imported into
#                                  a fresh store (failed imports leave dirty stores)
#
# Environment (all required unless noted):
#   STORE_DIR          store dir, e.g. /opt/lore/data/surreal/lore.db (prod) or
#                      ./data/surreal/lore.db (dev). Must be a directory.
#   SURREAL_CONTAINER  lore_surreal (prod) / nnp-lore-documents-writer-surreal-1 (dev)
#   APP_CONTAINERS     space-separated app containers to stop BEFORE the dump
#   BACKUP_DIR         dumps land here (prod /opt/lore/backups, dev ./backups)
#   PASS_FILE          the database password file secrets-init generated
#                      (prod /opt/lore/data/secrets/surreal/pass). The only
#                      password source — env SURREAL_PASS is wiring, not
#                      config (plan component-wiring-not-settings step 4);
#                      an explicit SURREAL_PASS is honored solely as a dev-
#                      rehearsal escape (the dev secrets live in a named
#                      volume with no host path).
#   SURREAL_USER/NS/DB optional overrides (default root / lore / main)
#   FORCE_SIZE_CHECK=1 optional: skip the dump-size-vs-history abort (legit shrink)
set -euo pipefail

MODE=""
RESUME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry ;;
    --confirm) MODE=real ;;
    --resume) shift; RESUME="${1:?--resume needs a dump path}"; MODE=real ;;
    --help|-h) sed -n '2,38p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "need --dry-run or --confirm (see --help)" >&2; exit 1; }

: "${STORE_DIR:?}" "${SURREAL_CONTAINER:?}" "${BACKUP_DIR:?}" "${APP_CONTAINERS:-}"
[ -d "$STORE_DIR" ] || { echo "store dir not found: $STORE_DIR" >&2; exit 1; }

log() { echo "[rebuild $(date +%H:%M:%S)] $*"; }
fail() { echo "[rebuild] ABORT: $*" >&2; exit 1; }

# curl container sharing surreal's netns + host paths (mkdir backup dir first)
sql_curl() { # $1 = surrealql body, rest = extra curl args
  docker run --rm --network "container:$SURREAL_CONTAINER" \
    -e SURREAL_USER -e SURREAL_PASS -e SURREAL_NS -e SURREAL_DB \
    curlimages/curl:latest -sS --fail-with-body \
    -u "$SURREAL_USER:$SURREAL_PASS" \
    -H "surreal-ns: $SURREAL_NS" -H "surreal-db: $SURREAL_DB" \
    -H "Accept: application/json" \
    --data-binary "$1" http://localhost:8000/sql "${@:2}"
}

# ── credentials: the password is the file secrets-init generated ────────────
if [ -z "${SURREAL_PASS:-}" ]; then
  : "${PASS_FILE:?set PASS_FILE (prod: /opt/lore/data/secrets/surreal/pass) or SURREAL_PASS for a dev rehearsal}"
  [ -f "$PASS_FILE" ] || { echo "password file not found: $PASS_FILE" >&2; exit 1; }
  SURREAL_PASS="$(cat "$PASS_FILE")"
fi
SURREAL_USER="${SURREAL_USER:-root}"
SURREAL_NS="${SURREAL_NS:-lore}"
SURREAL_DB="${SURREAL_DB:-main}"
[ -n "$SURREAL_PASS" ] || fail "empty database password (PASS_FILE=${PASS_FILE:-<unset>})"
export SURREAL_USER SURREAL_PASS SURREAL_NS SURREAL_DB

TS=$(date +%Y%m%d_%H%M%S)
PARENT=$(dirname "$STORE_DIR")
BASENAME=$(basename "$STORE_DIR")

# ── rail 1: pre-flight disk space (≥ 2.5x store size free on the volume FS) ────
store_mb=$(du -sm "$STORE_DIR" | cut -f1)
free_mb=$(df -Pm "$STORE_DIR" | awk 'NR==2 {print $4}')
need_mb=$((store_mb * 5 / 2))
log "store=${store_mb}MB free=${free_mb}MB need=${need_mb}MB (2.5x)"
[ "$free_mb" -ge "$need_mb" ] || fail "not enough free space (${free_mb}MB < ${need_mb}MB)"

container_up() { docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null | grep -q true; }

wait_healthy() { # surreal isready inside the container, up to 60s
  local i
  for i in $(seq 1 30); do
    docker exec "$SURREAL_CONTAINER" /surreal isready --endpoint http://localhost:8000 >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

verify_dump() { # $1 = dump.gz → rails 2+3
  gzip -t "$1" || fail "gzip -t failed: $1"
  # last non-empty line must END a complete INSERT: `... } ];` (space tolerated —
  # the closing sits on the same multi-MB line as the statement)
  gzip -dc "$1" | awk 'NF {last=$0} END {exit (last ~ /\}[[:space:]]*\];[[:space:]]*$/) ? 0 : 1}' \
    || fail "dump tail is not a complete INSERT statement — truncated dump: $1"
  # size sanity vs the newest prior dump: logical volume grows slowly; a collapse
  # means silent truncation (a 58MB truncation already fooled a human once)
  local prior new_mb prior_mb
  prior=$(ls -1t "$BACKUP_DIR"/dump-pre-deploy-*.surql.gz "$BACKUP_DIR"/rebuild-*.surql.gz 2>/dev/null | grep -v rebuild-dryrun | head -1 || true)
  if [ -n "$prior" ] && [ "${FORCE_SIZE_CHECK:-0}" != "1" ] && [ "$1" != "$prior" ]; then
    new_mb=$(du -m "$1" | cut -f1); prior_mb=$(du -m "$prior" | cut -f1)
    if [ "$new_mb" -le $((prior_mb / 2)) ]; then
      fail "dump ${new_mb}MB is <=50% of prior ${prior_mb}MB ($prior) — likely truncation. Inspect it; if the shrink is legit rerun with FORCE_SIZE_CHECK=1"
    fi
  fi
}

capture_counts() { # prints "<table> <count>" lines from the live DB via /sql
  local tables
  tables=$(sql_curl "INFO FOR DB;" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for r in d if isinstance(d,list) else [d]:
    if isinstance(r,dict) and isinstance(r.get("result"),dict) and "tables" in r["result"]:
        print("\n".join(sorted(r["result"]["tables"]))); break
') || fail "INFO FOR DB failed"
  [ -n "$tables" ] || fail "no tables returned by INFO FOR DB"
  python3 - "$tables" <<'EOF'
import subprocess, sys, os
env = dict(os.environ)
for t in sys.argv[1].split():
    q = f'SELECT count() FROM `{t}` GROUP ALL;'
    out = subprocess.run(
        ["docker","run","--rm","--network","container:"+os.environ["SURREAL_CONTAINER"],
         "-e","SURREAL_USER","-e","SURREAL_PASS","-e","SURREAL_NS","-e","SURREAL_DB",
         "curlimages/curl:latest","-sS","--fail-with-body",
         "-u",f'{os.environ["SURREAL_USER"]}:{os.environ["SURREAL_PASS"]}',
         "-H",f'surreal-ns: {os.environ["SURREAL_NS"]}',"-H",f'surreal-db: {os.environ["SURREAL_DB"]}',
         "-H","Accept: application/json","--data-binary",q,"http://localhost:8000/sql"],
        capture_output=True, text=True, check=True).stdout
    import json
    d = json.loads(out)
    n = 0
    for r in d if isinstance(d,list) else [d]:
        res = r.get("result") if isinstance(r,dict) else None
        if isinstance(res,list) and res and isinstance(res[0],dict) and "count" in res[0]:
            n = res[0]["count"]; break
    print(t, n)
EOF
}
export SURREAL_CONTAINER SURREAL_USER SURREAL_PASS SURREAL_NS SURREAL_DB

# docker -v bind mounts need absolute paths (dev runs use ./relative overrides)
mkdir -p "$BACKUP_DIR"
BACKUP_DIR=$(cd "$BACKUP_DIR" && pwd)
STORE_DIR=$(cd "$PARENT" && pwd)/"$BASENAME"
PARENT=$(dirname "$STORE_DIR")
DUMP="$BACKUP_DIR/rebuild-$TS.surql.gz"

if [ -n "$RESUME" ]; then
  # rail 4 (retry): reuse a VERIFIED COLD rebuild dump — never a dryrun one (taken hot)
  case "$RESUME" in
    "$BACKUP_DIR"/rebuild-*.surql.gz) ;;
    *) fail "--resume accepts only $BACKUP_DIR/rebuild-<ts>.surql.gz (cold, verified): got $RESUME" ;;
  esac
  case "$RESUME" in *dryrun*) fail "--resume refuses dryrun dumps (taken hot — not a restore candidate)" ;; esac
  [ -f "$RESUME" ] || fail "resume dump not found: $RESUME"
  DUMP="$RESUME"
  MODE=real
  log "RESUME mode: importing verified dump $DUMP into a fresh store"
fi

# ── dry-run: hot dump + verification only ──────────────────────────────────────
if [ "$MODE" = dry ]; then
  DUMP="$BACKUP_DIR/rebuild-dryrun-$TS.surql.gz"
  log "DRY-RUN: hot dump (apps stay up) → verify → before-counts. NO stop, NO import."
  log "dump: GET /export via netns curl → $DUMP"
  docker run --rm --network "container:$SURREAL_CONTAINER" \
    -e SURREAL_USER -e SURREAL_PASS -e SURREAL_NS -e SURREAL_DB \
    -v "$BACKUP_DIR":/mnt --user 0 --entrypoint sh curlimages/curl:latest -c '
      set -eu; set -o pipefail
      curl -sS --fail-with-body --max-time 1800 \
        -u "$SURREAL_USER:$SURREAL_PASS" \
        -H "surreal-ns: $SURREAL_NS" -H "surreal-db: $SURREAL_DB" \
        -H "Accept: application/octet-stream" \
        http://localhost:8000/export | gzip -c > /mnt/'"$(basename "$DUMP")"
  verify_dump "$DUMP"
  log "before-counts (live store):"
  capture_counts | tee "$BACKUP_DIR/rebuild-dryrun-$TS.counts"
  log "DRY-RUN complete: dump verified, counts captured. Dump kept as a fresh backup."
  exit 0
fi

# ══ REAL PATH (downtime starts) ════════════════════════════════════════════════
[ "$MODE" = real ] || fail "internal: bad mode"

if [ -z "$RESUME" ]; then
  [ -n "$APP_CONTAINERS" ] || fail "APP_CONTAINERS must list the app containers to stop"
  container_up "$SURREAL_CONTAINER" || fail "$SURREAL_CONTAINER not running"
  # ── stop app containers BEFORE the dump: closes the write window ─────────────
  log "stopping app containers: $APP_CONTAINERS"
  # shellcheck disable=SC2086
  docker stop $APP_CONTAINERS >/dev/null

  # ── dump (cold: apps stopped) ────────────────────────────────────────────────
  log "dump (cold): GET /export → $DUMP"
  docker run --rm --network "container:$SURREAL_CONTAINER" \
    -e SURREAL_USER -e SURREAL_PASS -e SURREAL_NS -e SURREAL_DB \
    -v "$BACKUP_DIR":/mnt --user 0 --entrypoint sh curlimages/curl:latest -c '
      set -eu; set -o pipefail
      curl -sS --fail-with-body --max-time 1800 \
        -u "$SURREAL_USER:$SURREAL_PASS" \
        -H "surreal-ns: $SURREAL_NS" -H "surreal-db: $SURREAL_DB" \
        -H "Accept: application/octet-stream" \
        http://localhost:8000/export | gzip -c > /mnt/'"$(basename "$DUMP")"
  verify_dump "$DUMP"
  log "dump verified (gzip, tail, size-vs-history)"

  log "before-counts (old store):"
  capture_counts | tee "$DUMP.counts"

  # ── stop surreal, mv store aside (rail 5: NEVER deleted) ─────────────────────
  log "stopping $SURREAL_CONTAINER"
  docker stop "$SURREAL_CONTAINER" >/dev/null
  ASIDE="$STORE_DIR.pre-rebuild-$TS"
  mv "$STORE_DIR" "$ASIDE"
  log "old store moved aside: $ASIDE"
else
  # resume: surreal may be up from the failed attempt — stop it, move partial aside
  if container_up "$SURREAL_CONTAINER"; then
    docker stop "$SURREAL_CONTAINER" >/dev/null
  fi
  if [ -d "$STORE_DIR" ]; then
    mv "$STORE_DIR" "$STORE_DIR.failed-import-attempt-$TS"
    log "dirty store moved aside: $STORE_DIR.failed-import-attempt-$TS"
  fi
  ASIDE=$(ls -1dt "$PARENT"/"$BASENAME".pre-rebuild-* 2>/dev/null | head -1 || true)
  [ -n "$ASIDE" ] || fail "resume: no lore.db.pre-rebuild-* found — nothing to import over?"
fi

# ── fresh store dir + boot surreal (rail 4: import only into a fresh dir) ──────
mkdir -p "$STORE_DIR"
docker start "$SURREAL_CONTAINER" >/dev/null
log "waiting for surreal healthy..."
wait_healthy || fail "surreal did not become healthy after start"
log "surreal healthy on fresh store"

# ── import ─────────────────────────────────────────────────────────────────────
log "import: POST /import ← $DUMP"
docker run --rm --network "container:$SURREAL_CONTAINER" \
  -e SURREAL_USER -e SURREAL_PASS -e SURREAL_NS -e SURREAL_DB \
  -v "$BACKUP_DIR":/mnt --user 0 --entrypoint sh curlimages/curl:latest -c '
    set -eu
    gunzip -c /mnt/'"$(basename "$DUMP")"' > /tmp/import.surql
    curl -sS --fail-with-body --max-time 3600 \
      -u "$SURREAL_USER:$SURREAL_PASS" \
      -H "surreal-ns: $SURREAL_NS" -H "surreal-db: $SURREAL_DB" \
      -H "Content-Type: application/octet-stream" \
      --data-binary @/tmp/import.surql http://localhost:8000/import
  ' || fail "IMPORT FAILED — dirty store left for inspection. Retry on a FRESH store:
  $0 --confirm --resume $DUMP"

# ── rail 6: table-count parity ─────────────────────────────────────────────────
log "after-counts (new store):"
capture_counts | tee "$DUMP.after-counts"
if ! diff -u <(sort "$DUMP.counts" 2>/dev/null || echo "NO-BEFORE-COUNTS") <(sort "$DUMP.after-counts"); then
  fail "PARITY MISMATCH — do NOT assume success. Old store is intact at $ASIDE.
  Rollback: docker stop $SURREAL_CONTAINER && rm -rf $STORE_DIR && mv $ASIDE $STORE_DIR && docker start $SURREAL_CONTAINER && docker start $APP_CONTAINERS"
fi
log "parity OK (all populated tables match)"

# ── bring apps back ────────────────────────────────────────────────────────────
# shellcheck disable=SC2086
docker start $APP_CONTAINERS >/dev/null
log "apps started. Rebuild complete."
log "new store size: $(du -sm "$STORE_DIR" | cut -f1)MB (was ${store_mb}MB)"
log "old store kept at: ${ASIDE} — delete manually after N days"
log "NOTE: if a deploy recreated the surreal container meanwhile, the watch
  state file baseline resets naturally (watch always overwrites it)."
