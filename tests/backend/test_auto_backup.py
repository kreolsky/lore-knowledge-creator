"""Unit tests for auto_backup dedup window — time-based dedup for auto-backups."""

from unittest.mock import AsyncMock, patch

import pytest
from auto_backup import (
    _compute_hash,
    validate_checkpoint_integrity,
)

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
