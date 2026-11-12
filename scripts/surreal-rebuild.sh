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
#   surreal-rebuild.sh --confirm --upgrade
#                                  engine change: the fresh store is booted on the
#                                  image COMPOSE names (not the running one); any
#                                  failure after the mv-aside rolls back by itself to
#                                  the old image on the old store and restarts the
#                                  apps. On success the apps stay STOPPED — they are
#                                  the old release; bring the new one up next.
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
#                      config; an explicit SURREAL_PASS is honored solely as a dev-
#                      rehearsal escape (the dev secrets live in a named
#                      volume with no host path).
#   SURREAL_USER/NS/DB optional overrides (default root / lore / main)
#   FORCE_SIZE_CHECK=1 optional: skip the dump-size-vs-history abort (legit shrink)
#   COMPOSE            --upgrade only: the compose command that owns surreal, with
#                      its -f named, e.g. `docker compose -p lore -f
#                      docker-compose.prod.yml` (the rollback adds a second -f)
set -euo pipefail

MODE=""
RESUME=""
UPGRADE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry ;;
    --confirm) MODE=real ;;
    --resume) shift; RESUME="${1:?--resume needs a dump path}"; MODE=real ;;
    --upgrade) UPGRADE=1 ;;
    --help|-h) sed -n '2,48p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done
[ -n "$MODE" ] || { echo "need --dry-run or --confirm (see --help)" >&2; exit 1; }
if [ -n "$UPGRADE" ]; then
  [ "$MODE" = real ] && [ -z "$RESUME" ] || { echo "--upgrade goes with --confirm alone" >&2; exit 1; }
  case " ${COMPOSE:-} " in *" -f "*) ;; *) echo "--upgrade needs COMPOSE with its -f (see --help)" >&2; exit 1 ;; esac
fi

: "${STORE_DIR:?}" "${SURREAL_CONTAINER:?}" "${BACKUP_DIR:?}" "${APP_CONTAINERS:-}"
[ -d "$STORE_DIR" ] || { echo "store dir not found: $STORE_DIR" >&2; exit 1; }

log() { echo "[rebuild $(date +%H:%M:%S)] $*"; }
# --upgrade runs unattended (the deploy), so a failure undoes what it already did:
# ROLLBACK=apps once the apps are stopped, =store once the old store is moved aside.
ROLLBACK=""
fail() {
  echo "[rebuild] ABORT: $*" >&2
  case "$ROLLBACK" in
    apps) ROLLBACK=""; docker start $APP_CONTAINERS >/dev/null; echo "[rebuild] apps restarted on the untouched store" >&2 ;;
    store) rollback_upgrade ;;
  esac
  exit 1
}
# WHY the trap: a command that dies under `set -e` (a failed cold /export, docker
# stop, mv) never reaches fail(), so without it the apps would stay stopped. It
# covers the main shell only — fail() inside a pipeline/$(…) subshell would roll
# back there and again here, so ROLLBACK-armed phases call capture_counts unpiped.
on_exit() { local rc=$?; [ "$rc" -eq 0 ] || [ -z "$ROLLBACK" ] || fail "unexpected exit $rc"; }
trap on_exit EXIT

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

# WHY: under --upgrade fail() rolls back by itself, so a manual-recovery recipe
# would name a store the rollback already moved — print it only without --upgrade.
manual_hint() { [ -n "$UPGRADE" ] || printf '\n  %s' "$1"; }

rollback_upgrade() { # back to the old image on the old store, apps restarted
  ROLLBACK=""
  echo "[rebuild] ROLLBACK: $NEW_IMAGE -> $OLD_IMAGE on $ASIDE" >&2
  docker rm -f "$SURREAL_CONTAINER" >/dev/null 2>&1 || true
  [ ! -e "$STORE_DIR" ] || mv "$STORE_DIR" "$STORE_DIR.failed-upgrade-$TS"
  mv "$ASIDE" "$STORE_DIR"
  local override
  override=$(mktemp)
  printf 'services:\n  surreal:\n    image: %s\n' "$OLD_IMAGE" > "$override"
  # WHY the override: the compose file already names the new image, and the
  # old container is gone — the old image comes back only by being named.
  $COMPOSE -f "$override" up -d --no-deps surreal
  rm -f "$override"
  wait_healthy || { echo "[rebuild] ROLLBACK FAILED: $OLD_IMAGE not healthy on $STORE_DIR" >&2; exit 2; }
  # shellcheck disable=SC2086
  docker start $APP_CONTAINERS >/dev/null
  echo "[rebuild] ROLLBACK done: $OLD_IMAGE serving the old store, apps started; failed store at $STORE_DIR.failed-upgrade-$TS" >&2
}

verify_dump() { # $1 = dump.gz → rails 2+3
  gzip -t "$1" || fail "gzip -t failed: $1"
  # last non-empty, non-comment line must END a statement with `;` (space tolerated —
  # an INSERT closes on the same multi-MB line it opens on, so a cut mid-line fails).
  # WHY not "must end an INSERT": tables export alphabetically and the last one
  # (ydoc_updates) is empty on a small install, so its dump ends on a DATA header.
  # A cut exactly between statements passes here; the per-table parity catches it.
  gzip -dc "$1" | awk 'NF && !/^--/ {last=$0} END {exit (last ~ /;[[:space:]]*$/) ? 0 : 1}' \
    || fail "dump tail is not a complete statement — truncated dump: $1"
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
  if [ -n "$UPGRADE" ]; then
    OLD_IMAGE=$(docker inspect -f '{{.Config.Image}}' "$SURREAL_CONTAINER")
    NEW_IMAGE=$($COMPOSE config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["surreal"]["image"])')
    [ "$OLD_IMAGE" != "$NEW_IMAGE" ] || fail "--upgrade: $SURREAL_CONTAINER already runs $NEW_IMAGE"
    log "upgrade $OLD_IMAGE -> $NEW_IMAGE; pulling before the downtime starts"
    $COMPOSE pull surreal || fail "could not pull $NEW_IMAGE"
  fi
  # ── stop app containers BEFORE the dump: closes the write window ─────────────
  log "stopping app containers: $APP_CONTAINERS"
  # shellcheck disable=SC2086
  docker stop $APP_CONTAINERS >/dev/null
  [ -z "$UPGRADE" ] || ROLLBACK=apps

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
  capture_counts > "$DUMP.counts"; cat "$DUMP.counts"

  # ── stop surreal, mv store aside (rail 5: NEVER deleted) ─────────────────────
  log "stopping $SURREAL_CONTAINER"
  docker stop "$SURREAL_CONTAINER" >/dev/null
  ASIDE="$STORE_DIR.pre-rebuild-$TS"
  mv "$STORE_DIR" "$ASIDE"
  [ -z "$UPGRADE" ] || ROLLBACK=store
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
if [ -n "$UPGRADE" ]; then
  log "booting $NEW_IMAGE on the fresh store"
  $COMPOSE up -d --no-deps surreal || fail "could not start $NEW_IMAGE"
else
  docker start "$SURREAL_CONTAINER" >/dev/null
fi
log "waiting for surreal healthy..."
wait_healthy || fail "surreal did not become healthy after start"
log "surreal healthy on fresh store"

# ── import ─────────────────────────────────────────────────────────────────────
log "import: POST /import ← $DUMP"
# Under --upgrade the dump's DDL is translated to what the new major accepts. Rows are
# untouched, so parity is unaffected. Two passes: the first reads only the dump's
# DEFINE FIELD lines (to learn which `X.*` have children), the second rewrites.
# Rule 1 — drop every DEFINE EVENT. WHY: a DEFINE EVENT is code, not data — only
# surreal/schema.surql defines one, and the new release's backend re-applies it at boot
# (OVERWRITE / IF NOT EXISTS) in the new engine's dialect. The old store's copy is the
# OLD release's body, which a new major may refuse ($parent is reserved on v4) and
# /import stops at the first refusal.
# Rule 2 — an `X.*` of TYPE any that has children becomes `object`, and its parent
# `array` becomes `array<object>`. WHY: v3 implied `X.* any` under a bare `array` and
# still held typed children under it; v4 /import accepts that shape, but v4 DEFINE then
# refuses any NEW child under it ("only an object holds a field") and refuses the
# retype on a populated SCHEMAFULL table without MIGRATE EXISTING ROWS — which needs a
# FALLBACK that passes the required children. Rewritten here, the store comes out in
# exactly the shape a fresh v4 builds from surreal/schema.surql. v3 enforced the typed
# children on every element, so every existing element is already an object.
UPGRADE_DDL_AWK=""
if [ -n "$UPGRADE" ]; then UPGRADE_DDL_AWK='
  function field() { i = 3; if ($i == "OVERWRITE") i++; NAME = $i; TBL = $(i + 2) }
  FNR == NR {
    if ($1 == "DEFINE" && $2 == "FIELD") {
      field(); s = NAME; p = ""
      while ((k = index(s, ".*.")) > 0) {
        p = p substr(s, 1, k + 1); has[TBL "|" p] = 1; p = p "."; s = substr(s, k + 3)
      }
    }
    next
  }
  !skip && /^DEFINE EVENT / { skip = 1; depth = 0 }
  skip { depth += gsub(/\{/, "{") - gsub(/\}/, "}")
         if (depth <= 0 && /;[[:space:]]*$/) { skip = 0; events++ }
         next }
  $1 == "DEFINE" && $2 == "FIELD" {
    field()
    if ((TBL "|" NAME) in has && sub(/ TYPE any /, " TYPE object ")) retyped++
    if ((TBL "|" NAME ".*") in has && (sub(/ TYPE none \| array /, " TYPE none | array<object> ") \
        || sub(/ TYPE array /, " TYPE array<object> "))) retyped++
  }
  { print }
  END {
    print "[rebuild] events dropped from the import (the new backend re-defines them): " events + 0 > "/dev/stderr"
    print "[rebuild] fields retyped to object under arrays: " retyped + 0 > "/dev/stderr"
  }'
fi
docker run --rm --network "container:$SURREAL_CONTAINER" \
  -e SURREAL_USER -e SURREAL_PASS -e SURREAL_NS -e SURREAL_DB -e UPGRADE_DDL_AWK="$UPGRADE_DDL_AWK" \
  -v "$BACKUP_DIR":/mnt --user 0 --entrypoint sh curlimages/curl:latest -c '
    set -eu
    if [ -n "$UPGRADE_DDL_AWK" ]; then
      gunzip -c /mnt/'"$(basename "$DUMP")"' | grep "^DEFINE FIELD " > /tmp/fields.surql || true
      gunzip -c /mnt/'"$(basename "$DUMP")"' | awk "$UPGRADE_DDL_AWK" /tmp/fields.surql - > /tmp/import.surql
    else
      gunzip -c /mnt/'"$(basename "$DUMP")"' > /tmp/import.surql
    fi
    curl -sS --fail-with-body --max-time 3600 \
      -u "$SURREAL_USER:$SURREAL_PASS" \
      -H "surreal-ns: $SURREAL_NS" -H "surreal-db: $SURREAL_DB" \
      -H "Content-Type: application/octet-stream" \
      -H "Accept: application/json" \
      --data-binary @/tmp/import.surql http://localhost:8000/import \
      > /mnt/'"$(basename "$DUMP")"'.import.json
  ' || fail "IMPORT FAILED (HTTP) — $(head -c 600 "$DUMP.import.json" 2>/dev/null).$(manual_hint "Dirty store left for inspection. Retry on a FRESH store: $0 --confirm --resume $DUMP")"
# WHY: /import answers per statement, so an HTTP 200 alone is not success — one
# refused row reports itself only as an ERR entry in the body.
if grep -Eq '"status": ?"ERR"' "$DUMP.import.json"; then
  fail "IMPORT REFUSED STATEMENTS — $(grep -o '"result":"[^"]*","status":"ERR"' "$DUMP.import.json" | head -3)"
fi

# ── rail 6: table-count parity ─────────────────────────────────────────────────
log "after-counts (new store):"
capture_counts > "$DUMP.after-counts"; cat "$DUMP.after-counts"
if ! diff -u <(sort "$DUMP.counts" 2>/dev/null || echo "NO-BEFORE-COUNTS") <(sort "$DUMP.after-counts"); then
  fail "PARITY MISMATCH — do NOT assume success.$(manual_hint "Old store is intact at $ASIDE. Rollback: docker stop $SURREAL_CONTAINER && rm -rf $STORE_DIR && mv $ASIDE $STORE_DIR && docker start $SURREAL_CONTAINER && docker start $APP_CONTAINERS")"
fi
log "parity OK (all populated tables match)"
ROLLBACK=""

if [ -n "$UPGRADE" ]; then
  log "upgrade complete on $NEW_IMAGE. Apps left STOPPED (old release): bring the new one up, e.g. $COMPOSE up -d"
  log "old store kept at: ${ASIDE} — delete manually after N days"
  exit 0
fi

# ── bring apps back ────────────────────────────────────────────────────────────
# shellcheck disable=SC2086
docker start $APP_CONTAINERS >/dev/null
log "apps started. Rebuild complete."
log "new store size: $(du -sm "$STORE_DIR" | cut -f1)MB (was ${store_mb}MB)"
log "old store kept at: ${ASIDE} — delete manually after N days"
log "NOTE: if a deploy recreated the surreal container meanwhile, the watch
  state file baseline resets naturally (watch always overwrites it)."
