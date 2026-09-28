"""cp-store convergence tasks — backfill + guarded null-out.

# see SYSTEM: cp-store — the convergence half of the inline-content dedup system.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def backfill_cp_blobs_task(ctx, batch_size: int = 50) -> None:
    """Backfill content_ref for legacy checkpoint rows that have inline content but no blob reference.

    For each unmigrated row: compress content into cp_blobs, set content_ref.
    Idempotent — skips rows that already have content_ref.

    INVARIANT: soft-deleted (deleted_at != NONE) rows are migrated too, NOT skipped.
    Why: their content lives in cp_blobs forever; the later null-out of inline `content`
    would destroy data on thinned rows that never got a content_ref.
    """
    from cp_store import hash_content as _hash_content
    from cp_store import put_content

    from db import extract_id, get_db

    db = await get_db()
    processed = 0
    while True:
        rows = await db.query(
            "SELECT id, content, content_hash FROM checkpoints "
            "WHERE content_ref IS NONE AND content IS NOT NONE "
            "LIMIT $limit",
            {"limit": batch_size},
        )
        if not rows:
            break
        # INVARIANT: every iteration must clear at least one row from the WHERE set,
        # else the same unmigratable rows (null id / null content) re-match forever.
        # Why: the LIMIT-batched loop would spin forever on rows it can't advance — the
        # `advanced` counter detects a stalled batch and breaks instead of looping.
        # Track progress and break when a full batch advanced nothing.
        advanced = 0
        for row in rows:
            content = row.get("content")
            if content is None:
                continue
            content_ref = await put_content(content)
            cp_id = extract_id(row.get("id"))
            if not cp_id:
                continue
            await db.query(
                "UPDATE type::record('checkpoints', $id) SET content_ref = $ref",
                {"id": cp_id, "ref": content_ref},
            )
            advanced += 1
            stored_hash = row.get("content_hash")
            if stored_hash:
                computed = _hash_content(content)
                if stored_hash != computed:
                    logger.warning(
                        "Hash mismatch on backfill for checkpoint %s: stored=%s computed=%s",
                        cp_id, stored_hash, computed,
                    )
            processed += 1
        if advanced == 0:
            logger.warning("cp_blobs backfill stalled: %d rows matched but none advanced", len(rows))
            break
        if len(rows) < batch_size:
            break
    logger.info("cp_blobs backfill complete: %d rows migrated", processed)


async def nullout_inline_content_task(ctx) -> None:
    """Irreversible null-out of legacy inline checkpoints.content, guard-gated.

    #   backfill_cp_blobs_task gives every legacy row a content_ref; this task then
    #   redefines content as option<string> and sets it NONE so the column stops
    #   double-storing every document body (~2× the blob payload). New rows stopped
    #   writing inline content at C3-A (cp_store.create_checkpoint), so this only
    #   clears the legacy tail.
    # ARCH: the irreversible op lives HERE (arq cron), NOT in the startup migration
    #   runner. Why: runner.py's contract is safe/idempotent/repeatable; an
    #   irreversible schema change needs a backup window, not a boot path. The
    #   startup runner only surfaces a readiness COUNT (see cp_nullout_ready_check);
    #   this task performs the op once backfill has converged.
    # INVARIANT: guarded by the SAME checks as the CLI migration — aborts (no-op +
    #   log) unless every row has a content_ref AND every blob resolves (hash matches).
    # Why: irreversible — nulling before blobs resolve destroys data, so a guard failure
    # is a soft "waiting for backfill" (no arq retry storm), not an error. Idempotent.
    """
    from db import get_db
    from migrations.migrate_cp_content_nullout import (
        _assert_blobs_resolve,
        _assert_fully_migrated,
        _perform_nullout,
    )

    db = await get_db()
    # Convergence short-circuit: once no row holds inline content, the migration is
    # done forever (new rows never write inline content post C3-A). Skip the O(N)
    # blob-resolve decompress loop + full-table UPDATE so the daily cron is a single
    # cheap count once converged, not a recurring full-table pass. Why: _assert_blobs_resolve
    # decompresses EVERY blob-backed row and _perform_nullout UPDATEs the whole table; both
    # keep matching every row after convergence (content_ref stays set), so without this gate
    # the one-shot migration guard becomes a permanent daily cost on a growing table.
    remaining = await db.query(
        "SELECT count() AS c FROM checkpoints WHERE content IS NOT NONE GROUP ALL",
        site="cp_nullout",
    )
    if not (remaining and remaining[0]["c"]):
        return
    try:
        await _assert_fully_migrated(db)
        await _assert_blobs_resolve(db)
    except RuntimeError as e:
        # Backfill not yet converged — wait for the next cron pass, do NOT null.
        logger.info("cp nullout skipped (waiting for backfill): %s", e)
        return
    await _perform_nullout(db)
    logger.info("cp nullout task: inline content nulled")
