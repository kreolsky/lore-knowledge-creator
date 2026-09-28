"""Unit tests for the session-start safety-on-open auto-backup trigger.

# CONTRACT (plan: session-start-snapshot-contract-and-label-registry, D1/D2):
# the open snapshot fires iff the current content hash differs from the NEWEST
# checkpoint's hash — ANY label (including last-session: a last-session row
# holding exactly this content already IS the session-start copy). There is NO
# age gate: a 3-day staleness gate was permanently held "fresh" by last-session /
# agent-auto / auto-backup rows on active documents, so it stopped firing at all.

Covers: fresh-but-different content (the F1 regression), last-session same/different
content, no-prior-checkpoint, legacy null hash, equal content at any age (skip),
reference/content-length guards.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from auto_backup import (
    BACKUP_MIN_CONTENT,
    _compute_hash,
    maybe_backup_on_open,
)

FRESH_CONTENT = "x" * BACKUP_MIN_CONTENT
FRESH_HASH = _compute_hash(FRESH_CONTENT)


def _row(content_hash: str | None = FRESH_HASH, minutes_ago: int = 10) -> dict:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return {"created_at": ts, "content_hash": content_hash}


def _writer_stub(*args, **kwargs):
    """Return a minimal checkpoint dict so emit/json_safe succeed on the create path."""
    return {"checkpoint_id": "cp1", "document_id": kwargs.get("document_id")}


class TestMaybeBackupOnOpen:
    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_creates_backup_when_latest_is_fresh_but_content_differs(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """F1 regression: a 10-minute-old checkpoint with DIFFERENT content must not
        suppress the session-start snapshot — the age gate is gone."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[_row(content_hash="old_hash", minutes_ago=10)])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is not None
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["label"] == "safety-open"

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_no_backup_when_last_session_row_holds_same_content(
        self, mock_get_db, mock_create,
    ):
        """D2: a newest-row (last-session included) with the SAME content hash IS the
        session-start copy — no redundant snapshot."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[_row(content_hash=FRESH_HASH, minutes_ago=2)])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is None
        mock_create.assert_not_called()

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_creates_backup_when_last_session_row_holds_different_content(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """D2: last-session content that diverged from the current state does NOT
        protect the new session — snapshot."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[_row(content_hash="diverged", minutes_ago=2)])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_creates_backup_when_no_prior_checkpoint(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.json_safe", return_value={})
    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_legacy_null_hash_creates_backup(
        self, mock_get_db, mock_create, mock_json, emit_recorder,
    ):
        """Null content_hash (legacy row) = can't compare → snapshot (safer branch)."""
        mock_db = AsyncMock()
        mock_db.query = AsyncMock(return_value=[_row(content_hash=None)])
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is not None
        mock_create.assert_called_once()

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_no_backup_for_references(
        self, mock_get_db, mock_create,
    ):
        result = await maybe_backup_on_open("doc1", FRESH_CONTENT, is_reference=True)
        assert result is None
        mock_create.assert_not_called()
        mock_get_db.assert_not_called()

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_no_backup_when_content_below_minimum(
        self, mock_get_db, mock_create,
    ):
        result = await maybe_backup_on_open("doc1", "short")
        assert result is None
        mock_create.assert_not_called()
        mock_get_db.assert_not_called()

    @patch("auto_backup.create_checkpoint", new_callable=AsyncMock, side_effect=_writer_stub)
    @patch("auto_backup.get_db", new_callable=AsyncMock)
    async def test_no_backup_when_ancient_checkpoint_has_equal_content(
        self, mock_get_db, mock_create,
    ):
        """The age gate is REMOVED, not inverted: equal content at ANY age means the
        state is already preserved — no snapshot."""
        mock_db = AsyncMock()
        ancient = datetime.now(timezone.utc) - timedelta(days=30)
        mock_db.query = AsyncMock(
            return_value=[{"created_at": ancient, "content_hash": FRESH_HASH}]
        )
        mock_get_db.return_value = mock_db

        result = await maybe_backup_on_open("doc1", FRESH_CONTENT)
        assert result is None
        mock_create.assert_not_called()
