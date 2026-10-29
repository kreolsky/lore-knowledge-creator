"""Tests for the arq auto_backup_loss_task and the collab/documents trigger migration.

Phase F migration: the hot collab-flush path enqueues auto_backup_loss_task via arq.
The documents.py REST PATCH path stays INLINE — its client is not on the collab WS,
so it relies on the synchronous `auto_backup` field in the response to surface the
snapshot. maybe_backup_on_editor_handoff stays inline (synchronous path).
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_auto_backup_loss_task_no_baseline():
    """auto_backup_loss_task passes None baseline when not provided."""
    from jobs.tasks import auto_backup_loss_task

    with patch("auto_backup.maybe_backup_on_content_loss", new_callable=AsyncMock) as mock_backup:
        await auto_backup_loss_task({}, "doc-2", "new content", None, None)

    mock_backup.assert_awaited_once_with(
        "doc-2", "new content", baseline_content=None, baseline_tables_json=None,
    )
