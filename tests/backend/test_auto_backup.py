"""Unit tests for auto_backup dedup window — time-based dedup for auto-backups."""

from unittest.mock import AsyncMock, patch

import pytest
from auto_backup import (
    AUTO_BACKUP_DEDUP_WINDOW_SEC,
    _compute_hash,
    maybe_backup_on_content_loss,
    maybe_backup_on_editor_handoff,
    validate_checkpoint_integrity,
)

from config import FLUSH_INTERVAL_SEC


class TestAutoBackupDedup:
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_auto_backup_within_window_returns_none(self, mock_get_db):
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[1])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc1", "",
            baseline_content="x" * 500,
        )
        assert result is None

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_manual_checkpoint_does_not_block(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        mock_db = AsyncMock()
        # 1st query = dedup-window count (0 → proceed); 2nd = latest-checkpoint
        # hash lookup ([] → no prior checkpoint → not a duplicate).
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": "baseline"}

        baseline = "x" * 500
        result = await maybe_backup_on_content_loss(
            "doc1", "short", baseline_content=baseline,
        )
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_auto_backup_outside_window_returns_backup(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(side_effect=[[0], []])
        mock_get_db.return_value = mock_db
        mock_create.return_value = {"checkpoint_id": "cp1", "content": "baseline"}

        baseline = "x" * 500
        result = await maybe_backup_on_content_loss(
            "doc1", "short", baseline_content=baseline,
        )
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_content_loss_skips_when_equals_latest_checkpoint(
        self, mock_get_db, mock_create,
    ):
        """An identical backup that fell outside the time window is still skipped
        when its hash equals the latest existing checkpoint (no adjacent dupes)."""
        baseline = "x" * 500
        baseline_hash = _compute_hash(baseline)
        mock_db = AsyncMock()
        # window count = 0 (outside window), but latest checkpoint hash matches.
        mock_db.query = AsyncMock(
            side_effect=[[0], [{"content_hash": baseline_hash, "created_at": "now"}]]
        )
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_content_loss(
            "doc1", "short", baseline_content=baseline,
        )
        assert result is None
        mock_create.assert_not_called()

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_handoff_dedup_vs_latest(self, mock_get_db, mock_create):
        """maybe_backup_on_editor_handoff returns None when content already equals
        the latest checkpoint hash."""
        content = "y" * 100
        content_hash = _compute_hash(content)
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(
            return_value=[{"content_hash": content_hash, "created_at": "now"}]
        )
        mock_get_db.return_value = mock_db

        result, returned_hash = await maybe_backup_on_editor_handoff(
            "doc1", content, "user-A", "Alice",
        )
        assert result is None
        assert returned_hash == content_hash
        mock_create.assert_not_called()

    def test_dedup_window_derived_from_flush_interval(self):
        assert AUTO_BACKUP_DEDUP_WINDOW_SEC > 2 * FLUSH_INTERVAL_SEC


# ─── F4: validate distinguishes transient blob failure from corruption ──────


@pytest.mark.asyncio
@patch("cp_store.get_content", new_callable=AsyncMock)
async def test_validate_blob_unreadable_when_ref_set_and_resolution_raises(mock_get):
    """F4: a checkpoint with a content_ref whose blob resolution raises, and with NO
    inline content, must NOT hash the empty fallback. It returns valid: None +
    reason: blob_unreadable (transient failure is not corruption). The resolution
    failure is injected at the blob layer (cp_store.get_content) so the shared
    resolve_checkpoint_content helper exercises its real fallback logic."""
    mock_get.side_effect = ValueError("not found")
    record = {
        "content_ref": "deadbeef" * 8,
        "content_hash": "deadbeef" * 8,
        "content": None,  # post-nullout: no inline fallback
    }
    result = await validate_checkpoint_integrity("cp1", record=record)
    assert result["valid"] is None
    assert result["reason"] == "blob_unreadable"


@pytest.mark.asyncio
@patch("cp_store.get_content", new_callable=AsyncMock)
async def test_validate_uses_inline_when_ref_fails_with_inline_present(mock_get):
    """F4: a legacy row that DOES carry inline content still validates via the
    fallback — blob_unreadable only applies when inline is genuinely absent."""
    mock_get.side_effect = ValueError("not found")
    stored = "inline legacy body"
    stored_hash = _compute_hash(stored)
    record = {
        "content_ref": "deadbeef" * 8,
        "content_hash": stored_hash,
        "content": stored,
    }
    result = await validate_checkpoint_integrity("cp1", record=record)
    assert result["valid"] is True
