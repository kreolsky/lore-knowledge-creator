"""GFS thinning of auto-label checkpoints — see SYSTEM: auto_backup."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


async def thin_auto_checkpoints_task(ctx) -> None:
    """Thin old auto-label checkpoints via GFS-style retention.

    # INVARIANT: only AUTO-label checkpoints (auto-backup, editor-handoff, safety-open)
    # may be soft-deleted; manual snapshots, the per-doc _backup row, and blobs are immortal.
    # Why: user rule — thin auto history only, never destroy data; _backup is the only
    # undo-last-restore point and never accumulates.

    Retention tiers (per document):
      1. Recent window (THIN_RECENT_HOURS): keep ALL auto checkpoints.
      2. Daily window (THIN_DAILY_KEEP_DAYS after recent): keep 1 per calendar day (newest).
      3. Weekly window (THIN_WEEKLY_KEEP_WEEKS after daily): keep 1 per calendar week (newest).
      4. Older than all tiers: soft-delete everything.

    Idempotent — already-deleted rows are filtered out.

    ARCH: processes per-document to avoid unbounded memory allocation. Each
    document's checkpoints are fetched in a bounded per-document query (one
    document's worth of rows, NO LIMIT because the within-doc count is itself
    bounded by thinning over time). The soft-delete batches are flushed inside the
    per-doc loop whenever they reach the batch size, so memory is bounded across
    documents too — a killed task leaves idempotent partial work (end-state after a
    full run is unchanged; only the delete granularity differs).
    """
    import settings
    from auto_backup import AUTO_LABELS

    from db import extract_id, get_db

    db = await get_db()
    labels = sorted(AUTO_LABELS)

    # Label membership is a single `label IN $labels` predicate (params, not
    # string interpolation) — used in both the per-doc scan and the doc grouping.
    doc_rows = await db.query(
        "SELECT document_id FROM checkpoints "
        "WHERE label IN $labels AND deleted_at IS NONE "
        "GROUP BY document_id",
        {"labels": labels},
    )
    if not doc_rows:
        return

    doc_ids = [r["document_id"] for r in doc_rows]

    now = datetime.now(timezone.utc)
    thinning = await settings.get_all(
        ["THIN_RECENT_HOURS", "THIN_DAILY_KEEP_DAYS", "THIN_WEEKLY_KEEP_WEEKS"]
    )
    recent_cutoff = now - timedelta(hours=thinning["THIN_RECENT_HOURS"])
    daily_cutoff = recent_cutoff - timedelta(days=thinning["THIN_DAILY_KEEP_DAYS"])
    weekly_cutoff = daily_cutoff - timedelta(weeks=thinning["THIN_WEEKLY_KEEP_WEEKS"])

    to_delete: list[str] = []
    batch_size = 50
    total_deleted = 0
    total_docs = len(doc_ids)

    for doc_idx, doc_id in enumerate(doc_ids):
        cp_rows = await db.query(
            "SELECT id, created_at FROM checkpoints "
            "WHERE document_id = $did AND label IN $labels AND deleted_at IS NONE "
            "ORDER BY created_at DESC",
            {"did": doc_id, "labels": labels},
        )
        if not cp_rows:
            continue

        keep_ids: set[str] = set()
        seen_days: set[str] = set()
        seen_weeks: set[str] = set()

        for row in cp_rows:
            cp_id = extract_id(row.get("id", ""))
            if not cp_id:
                continue

            created_at = row.get("created_at")
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at)
            if created_at and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if created_at is None:
                continue

            if created_at >= recent_cutoff:
                keep_ids.add(cp_id)
                continue

            if created_at >= daily_cutoff:
                day_key = created_at.strftime("%Y-%m-%d")
                if day_key not in seen_days:
                    seen_days.add(day_key)
                    keep_ids.add(cp_id)
                continue

            if created_at >= weekly_cutoff:
                iso = created_at.isocalendar()
                week_key = f"{iso[0]}-W{iso[1]:02d}"
                if week_key not in seen_weeks:
                    seen_weeks.add(week_key)
                    keep_ids.add(cp_id)
                continue

        for row in cp_rows:
            cp_id = extract_id(row.get("id", ""))
            if cp_id and cp_id not in keep_ids:
                to_delete.append(cp_id)

        # Flush the soft-delete batch incrementally — `while` (not `if`) so a
        # single document's deletes are FULLY drained in 50-id chunks. The per-doc
        # scan has no LIMIT, so an `if` that drained only one batch would leave a
        # net +(D-50) on the list per high-volume doc → unbounded growth (contradicting
        # the ARCH comment). `while` bounds memory to ≤ batch_size between docs and lets
        # a killed task leave idempotent partial work (the per-id UPDATE is idempotent on
        # deleted_at). A final flush after the loop handles the remainder.
        while len(to_delete) >= batch_size:
            total_deleted += await _flush_thin_deletes(db, to_delete[:batch_size])
            del to_delete[:batch_size]

        if (doc_idx + 1) % 500 == 0:
            logger.info("Thinning progress: %d/%d docs processed, %d deleted so far",
                        doc_idx + 1, total_docs, total_deleted)

    if not to_delete:
        logger.info("Thinning: no auto checkpoints to thin")
        return

    total_deleted += await _flush_thin_deletes(db, to_delete)

    logger.info("Thinning: soft-deleted %d auto checkpoints across %d documents", total_deleted, total_docs)


async def _flush_thin_deletes(db, ids: list[str]) -> int:
    """Soft-delete a batch of checkpoint ids; return how many were deleted."""
    if not ids:
        return 0
    await db.query(
        "UPDATE checkpoints SET deleted_at = time::now() "
        "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
        {"ids": ids},
    )
    return len(ids)
