"""Tests for the arq auto_backup_loss_task and the collab/documents trigger migration.

Phase F migration: the hot collab-flush path enqueues auto_backup_loss_task via arq.
The documents.py REST PATCH path stays INLINE — its client is not on the collab WS,
so it relies on the synchronous `auto_backup` field in the response to surface the
snapshot. maybe_backup_on_editor_handoff stays inline (synchronous path).
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_auto_backup_loss_task_delegates():
    """auto_backup_loss_task calls maybe_backup_on_content_loss with args."""
    from jobs.tasks import auto_backup_loss_task

    with patch("auto_backup.maybe_backup_on_content_loss", new_callable=AsyncMock) as mock_backup:
        await auto_backup_loss_task(
            {}, "doc-1", "new content", "old content baseline", "{}",
        )

    mock_backup.assert_awaited_once_with(
        "doc-1", "new content",
        baseline_content="old content baseline", baseline_tables_json="{}",
    )


@pytest.mark.asyncio
async def test_auto_backup_loss_task_no_baseline():
    """auto_backup_loss_task passes None baseline when not provided."""
    from jobs.tasks import auto_backup_loss_task

    with patch("auto_backup.maybe_backup_on_content_loss", new_callable=AsyncMock) as mock_backup:
        await auto_backup_loss_task({}, "doc-2", "new content", None, None)

    mock_backup.assert_awaited_once_with(
        "doc-2", "new content", baseline_content=None, baseline_tables_json=None,
    )


@pytest.mark.asyncio
async def test_auto_backup_handoff_task_delegates():
    """auto_backup_handoff_task calls maybe_backup_on_editor_handoff with args."""
    from jobs.tasks import auto_backup_handoff_task

    with patch("auto_backup.maybe_backup_on_editor_handoff", new_callable=AsyncMock) as mock_backup:
        await auto_backup_handoff_task(
            {}, "doc-1", "pre content",
            from_user_id="user-A", from_user_name="Alice", tables_json="{}",
        )

    mock_backup.assert_awaited_once_with(
        "doc-1", "pre content", from_user_id="user-A", from_user_name="Alice",
        tables_json="{}",
    )


@pytest.mark.asyncio
async def test_auto_backup_open_task_delegates():
    """auto_backup_open_task calls maybe_backup_on_open with args."""
    from jobs.tasks import auto_backup_open_task

    with patch("auto_backup.maybe_backup_on_open", new_callable=AsyncMock) as mock_backup:
        await auto_backup_open_task(
            {}, "doc-1", "some content", False, "fake_hash", "{}",
        )

    mock_backup.assert_awaited_once_with(
        "doc-1", "some content", tables_json="{}",
        is_reference=False, _precomputed_hash="fake_hash",
    )


@pytest.mark.asyncio
async def test_documents_patch_backup_inline_not_enqueued(client, admin_user, project_with_doc, enqueue_recorder):
    """PATCH /api/documents/{id} with a dangerous content change creates the backup
    INLINE (no arq enqueue) and returns it in the response so the REST client can
    surface the snapshot live."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    sentinel_backup = {"checkpoint_id": "cp-1", "content_hash": "abc"}

    with patch("auto_backup.maybe_backup_on_content_loss",
               new_callable=AsyncMock, return_value=sentinel_backup) as mock_backup:
        resp = await client.patch(
            f"/api/documents/{doc_id}",
            json={"content": "Short"},
            cookies={"lore_session": token},
        )

    assert resp.status_code == 200
    # documents path must NOT enqueue a backup job
    assert "auto_backup_loss_task" not in enqueue_recorder.names()
    mock_backup.assert_awaited_once()
    assert resp.json().get("auto_backup") == sentinel_backup
