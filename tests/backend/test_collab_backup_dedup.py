"""4.1 — CollabSession skips the redundant auto_backup enqueue roundtrip.

Two consecutive flushes that share a baseline (for example, a failing/retrying flush re-deriving
the same _last_flushed_content) compute the same backup job_id; arq already dedups on it,
but we suppress the RPC entirely via _last_backup_job_id.
"""

from unittest.mock import AsyncMock

import pytest
from collab.session import CollabSession
from enqueue_recorder import EnqueueRecorder
from pycrdt import Doc, Text


def _mk_session() -> CollabSession:
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello"
    return CollabSession(entity_type="doc", entity_id="d1", ydoc=doc)


@pytest.mark.asyncio
async def test_same_baseline_enqueues_once():
    session = _mk_session()
    db = AsyncMock()
    with EnqueueRecorder.active() as mock_enqueue:
        await session._flush_pipeline._flush_content_to_db(db, "hello world")
        await session._flush_pipeline._flush_content_to_db(db, "hello world again")
    # Baseline (_last_flushed_content) never changed between calls → same job_id → one enqueue.
    assert len(mock_enqueue.calls) == 1


@pytest.mark.asyncio
async def test_changed_baseline_enqueues_again():
    session = _mk_session()
    db = AsyncMock()
    with EnqueueRecorder.active() as mock_enqueue:
        await session._flush_pipeline._flush_content_to_db(db, "v1")
        session._last_flushed_content = "v1"  # simulate a successful flush updating baseline
        await session._flush_pipeline._flush_content_to_db(db, "v2")
    assert len(mock_enqueue.calls) == 2
