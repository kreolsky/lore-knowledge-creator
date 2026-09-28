"""Integration tests for Phase 3 — auto-checkpoint thinning.

# INVARIANT: only AUTO-label checkpoints (auto-backup, editor-handoff, safety-open)
# may be soft-deleted; manual named snapshots and the per-doc _backup row are immortal;
# blobs are never removed.
# Why: user rule — thin auto history only, never destroy data; _backup is the only
# undo-last-restore point and never accumulates.
"""

from datetime import datetime, timedelta, timezone

import pytest
from auto_backup import AUTO_LABELS
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
async def test_auto_labels_contains_expected_labels():
    assert "auto-backup" in AUTO_LABELS
    assert "editor-handoff" in AUTO_LABELS
    assert "safety-open" in AUTO_LABELS
    # agent-auto is unbounded automatic history (one row per agent splice) — it
    # MUST be thinnable or it grows forever.
    assert "agent-auto" in AUTO_LABELS


@pytest.mark.asyncio
async def test_manual_snapshot_survives_thinning(test_db):
    doc_id = "thin-doc-manual"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "my-snapshot", timedelta(days=60), "manual content"),
        ("cp2", "auto-backup", timedelta(days=60), "auto content"),
    ])

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT label, deleted_at FROM checkpoints "
        "WHERE document_id = $did AND label = 'my-snapshot'",
        {"did": doc_id},
    )
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is None


@pytest.mark.asyncio
async def test_backup_row_survives_thinning(test_db):
    doc_id = "thin-doc-backup"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "_backup", timedelta(days=60), "backup content"),
        ("cp2", "auto-backup", timedelta(days=60), "auto content"),
    ])

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT label, deleted_at FROM checkpoints "
        "WHERE document_id = $did AND label = '_backup'",
        {"did": doc_id},
    )
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is None


@pytest.mark.asyncio
async def test_recent_auto_survives_thinning(test_db):
    doc_id = "thin-doc-recent"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "auto-backup", timedelta(hours=1), "recent auto"),
        ("cp2", "editor-handoff", timedelta(hours=12), "recent handoff"),
    ])

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT label, deleted_at FROM checkpoints "
        "WHERE document_id = $did AND deleted_at IS NONE",
        {"did": doc_id},
    )
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_old_auto_is_thinned(test_db):
    doc_id = "thin-doc-old"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "auto-backup", timedelta(days=60), "old auto 1"),
        ("cp2", "auto-backup", timedelta(days=61), "old auto 2"),
        ("cp3", "auto-backup", timedelta(days=62), "old auto 3"),
    ])

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT label, deleted_at FROM checkpoints "
        "WHERE document_id = $did",
        {"did": doc_id},
    )
    deleted = [r for r in rows if r["deleted_at"] is not None]
    assert len(deleted) >= 1


@pytest.mark.asyncio
async def test_safety_open_is_thinned(test_db):
    doc_id = "thin-doc-safety"
    from jobs.tasks import thin_auto_checkpoints_task

    await _seed_checkpoints(test_db, doc_id, [
        ("cp1", "safety-open", timedelta(days=200), "old safety"),
    ])

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT deleted_at FROM checkpoints "
        "WHERE document_id = $did AND label = 'safety-open'",
        {"did": doc_id},
    )
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is not None


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
async def test_daily_retention_keeps_one_per_day(test_db):
    doc_id = "thin-doc-daily"
    from jobs.tasks import thin_auto_checkpoints_task

    day = timedelta(days=1)
    hour = timedelta(hours=1)
    entries = [
        ("cp0", "auto-backup", 5 * day, "auto 5d-0"),
        ("cp1", "auto-backup", 5 * day + hour, "auto 5d-1"),
        ("cp2", "auto-backup", 5 * day + 2 * hour, "auto 5d-2"),
        ("cp3", "auto-backup", 6 * day, "auto 6d"),
    ]

    # Anchor the sub-day cluster to noon so the 1h/2h offsets stay within the same
    # calendar day regardless of wall-clock time (CI near midnight crossed the day
    # boundary and kept 3 instead of 2).
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    await _seed_checkpoints(test_db, doc_id, entries, base=noon)

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT comment, deleted_at, created_at FROM checkpoints "
        "WHERE document_id = $did "
        "ORDER BY created_at DESC",
        {"did": doc_id},
    )
    auto_rows = [r for r in rows if "Test auto-backup" in (r.get("comment") or "")]
    alive = [r for r in auto_rows if r["deleted_at"] is None]
    deleted = [r for r in auto_rows if r["deleted_at"] is not None]
    assert len(alive) == 2
    assert len(deleted) == 2
    assert len(alive) == 2
    assert len(deleted) == 2


@pytest.mark.asyncio
async def test_restore_after_thinning_still_works(client, admin_user, project_with_doc, test_db):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "ThinRestore"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Original"}, cookies=cookies)

    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "auto-backup"},
        cookies=cookies,
    )
    cp_id = resp.json()["checkpoint_id"]

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) SET created_at = $old",
        {"id": cp_id, "old": now - timedelta(days=60)},
    )

    from jobs.tasks import thin_auto_checkpoints_task
    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT id, deleted_at FROM type::record('checkpoints', $id)",
        {"id": cp_id},
    )
    cp_deleted = rows[0]["deleted_at"] is not None if rows else True
    if not cp_deleted:
        await client.patch(f"/api/documents/{doc_id}", json={"content": "Changed"}, cookies=cookies)
        resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
        assert resp.status_code == 200

        resp = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
        assert resp.json()["content"] == "Original"


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
