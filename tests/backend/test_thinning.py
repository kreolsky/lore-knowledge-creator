"""Integration tests for Phase 3 — auto-checkpoint thinning.

# INVARIANT: only AUTO-label checkpoints (auto-backup, editor-handoff, safety-open)
# may be soft-deleted; manual named snapshots and the per-doc _backup row are immortal;
# blobs are never removed.
# Why: user rule — thin auto history only, never destroy data; _backup is the only
# undo-last-restore point and never accumulates.
"""

from datetime import datetime, timedelta, timezone

import pytest
from cp_store import hash_content


async def _seed_checkpoints(test_db, doc_id, entries, base=None):
    """Create checkpoint rows directly in DB.

    entries: list of (id_suffix, label, created_at_ago: timedelta, content_text)
    base: anchor for `created_at = base - ago`; defaults to now. Pass a noon-anchored
    base for sub-day spacing so hour offsets never straddle a calendar-day boundary.
    """
    from db import create_record

    anchor = base if base is not None else datetime.now(timezone.utc)
    ids = []
    for suffix, label, ago, text in entries:
        cp_id = f"thin-{doc_id}-{suffix}"
        await create_record("checkpoints", cp_id, {
            "document_id": doc_id,
            "content": text,
            "content_hash": hash_content(text),
            "content_ref": hash_content(text),
            "label": label,
            "comment": f"Test {label}",
            "created_at": anchor - ago,
        })
        ids.append(cp_id)
    return ids


@pytest.mark.asyncio
async def test_blobs_not_touched_by_thinning(test_db):
    doc_id = "thin-doc-blobs"
    from cp_store import put_content

    from jobs.tasks import thin_auto_checkpoints_task

    text = "Content with blob"
    blob_hash = await put_content(text)
    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "auto-backup", timedelta(days=200), text),
    ])

    await thin_auto_checkpoints_task({})

    blobs = await test_db.query(
        "SELECT id FROM type::record('cp_blobs', $ref)",
        {"ref": blob_hash},
    )
    assert blobs


@pytest.mark.asyncio
async def test_thinning_is_idempotent(test_db):
    doc_id = "thin-doc-idem"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "auto-backup", timedelta(days=60), "old auto 1"),
        ("cp2", "auto-backup", timedelta(days=30), "old auto 2"),
        ("cp3", "manual", timedelta(days=60), "manual content"),
    ])

    await thin_auto_checkpoints_task({})
    rows_after_1 = await test_db.query(
        "SELECT count() AS c FROM checkpoints WHERE document_id = $did AND deleted_at IS NONE",
        {"did": doc_id},
    )
    alive_1 = rows_after_1[0]["c"] if rows_after_1 else 0

    await thin_auto_checkpoints_task({})
    rows_after_2 = await test_db.query(
        "SELECT count() AS c FROM checkpoints WHERE document_id = $did AND deleted_at IS NONE",
        {"did": doc_id},
    )
    alive_2 = rows_after_2[0]["c"] if rows_after_2 else 0

    assert alive_1 == alive_2
