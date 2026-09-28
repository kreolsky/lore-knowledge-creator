"""Data migration: null out inline checkpoints.content after blob backfill.

Plan: ~/.claude/plans/checkpoint-retention-growth.md (Phase 1, final irreversible step).

ARCH: One-time, irreversible data migration — the last Phase-1 step. After it, a
checkpoint's text lives ONLY in cp_blobs (resolved via content_ref); the inline
`content` dual-read fallback is gone. Run only after backfill_cp_blobs_task has set
content_ref on every row and a verification window confirmed every row resolves.

INVARIANT: redefine checkpoints.content to option<string> with DEFINE FIELD OVERWRITE
BEFORE the null-out. Why: existing DBs were created with `content TYPE string`
(non-option); schema.surql uses DEFINE FIELD IF NOT EXISTS, which does NOT redefine an
existing field — so the field stays non-option in the live DB even after the schema
file changed. Writing NONE into a non-option field fails a coerce error that db.query()
silently swallows (the exact trap in .claude/rules/backend.md), so the UPDATE acks but
nulls nothing. OVERWRITE makes the type change actually land.

Safety: aborts (no null-out) if ANY row still has inline content without a content_ref,
or if ANY blob fails to resolve / hash-mismatches. Idempotent: re-running after a clean
null-out is a no-op (0 rows left to null).

Usage (back up + run backfill first; never -v against production):
    docker compose exec backend python -m migrations.migrate_cp_content_nullout
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from cp_store import get_content, hash_content

from db import get_db

logger = logging.getLogger("migrate_cp_content_nullout")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def _assert_fully_migrated(db) -> None:
    """Abort unless every inline-content row has a content_ref (no data would be lost)."""
    rows = await db.query(
        "SELECT count() AS c FROM checkpoints "
        "WHERE content IS NOT NONE AND content_ref IS NONE GROUP ALL"
    )
    unmigrated = rows[0]["c"] if rows else 0
    if unmigrated:
        raise RuntimeError(
            f"{unmigrated} checkpoint row(s) hold inline content without a content_ref — "
            "run backfill_cp_blobs_task first; refusing to null out (would lose data)."
        )


async def _assert_blobs_resolve(db) -> None:
    """Abort unless every content_ref resolves and its hash matches (restorability gate)."""
    rows = await db.query(
        "SELECT id, content_ref FROM checkpoints WHERE content_ref IS NOT NONE"
    )
    fail = mismatch = 0
    for row in rows or []:
        ref = row["content_ref"]
        try:
            text = await get_content(ref)
        except Exception as e:
            fail += 1
            logger.error("Blob %s for checkpoint %s does not resolve: %s", ref, row["id"], e)
            continue
        if hash_content(text) != ref:
            mismatch += 1
            logger.error("Hash mismatch for checkpoint %s (ref %s)", row["id"], ref)
    if fail or mismatch:
        raise RuntimeError(
            f"verification failed: {fail} unresolved, {mismatch} hash-mismatched — "
            "refusing to null out inline content."
        )
    logger.info("Verification OK: %d blob-backed rows resolve with matching hash", len(rows or []))


async def _perform_nullout(db) -> None:
    """Irreversible null-out: redefine content as option<string>, then set it NONE.

    Assumes the guards (_assert_fully_migrated + _assert_blobs_resolve) have already
    passed. Split out so the arq cron task and the CLI share one implementation.
    """
    # WHY (see module docstring): OVERWRITE to option<string> BEFORE nulling,
    # else the existing non-option field silently rejects NONE via a swallowed coerce error.
    redefine = await db.query_raw(
        "DEFINE FIELD OVERWRITE content ON checkpoints TYPE option<string>;"
    )
    status = redefine["result"][0]["status"] if redefine.get("result") else "?"
    if status != "OK":
        raise RuntimeError(f"field redefine failed: {redefine}")
    logger.info("Redefined checkpoints.content as option<string>")

    nullout = await db.query_raw(
        "UPDATE checkpoints SET content = NONE WHERE content_ref IS NOT NONE;"
    )
    status = nullout["result"][0]["status"] if nullout.get("result") else "?"
    if status != "OK":
        raise RuntimeError(f"null-out UPDATE failed: {nullout}")

    remaining = await db.query(
        "SELECT count() AS c FROM checkpoints WHERE content IS NOT NONE GROUP ALL"
    )
    left = remaining[0]["c"] if remaining else 0
    if left:
        raise RuntimeError(f"null-out incomplete: {left} row(s) still hold inline content")

    logger.info("Null-out complete: inline content removed; all text now in cp_blobs")


async def run_migration():
    logger.info("Starting checkpoints.content null-out migration...")
    db = await get_db()

    await _assert_fully_migrated(db)
    await _assert_blobs_resolve(db)
    await _perform_nullout(db)


if __name__ == "__main__":
    asyncio.run(run_migration())
