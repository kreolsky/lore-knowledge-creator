"""Tests for flush degradation signalling — PR-2 (R2).

Covers: CollabSession tracks consecutive flush failures; broadcasts
save_degraded on 3 consecutive failures; broadcasts save_recovered
on the first successful flush after degradation. Threshold prevents
flapping on single transient errors.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from collab.registry import _sessions
from collab.session import CollabSession, ConnectedClient
from enqueue_recorder import EnqueueRecorder
from pycrdt import Doc, Text


def _mk_session(**kw) -> CollabSession:
    # CRDT: content is derived from the Y.Doc. Seed "hello" into the doc; leave
    # _last_flushed_content at its "" default so flush_to_db sees a change and
    # actually attempts the (mocked-to-fail) DB write.
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello"
    doc._lore_text = text
    return CollabSession(entity_type="doc", entity_id="d1", ydoc=doc, **kw)


def _mk_client(user_id="u1", user_name="User1", access_level="full") -> ConnectedClient:
    ws = AsyncMock()
    ws.close = AsyncMock()
    client = ConnectedClient(ws=ws, user_id=user_id, user_name=user_name, access_level=access_level)
    return client


@pytest.fixture(autouse=True)
def _clear_sessions():
    _sessions.clear()
    yield
    for s in _sessions.values():
        s.stop_periodic_flush()
        if s._batch_task and not s._batch_task.done():
            s._batch_task.cancel()
    _sessions.clear()


class TestFlushCompactGate:
    """§3.3 optimization: flush runs maybe_compact only after enough local
    appends accumulate, avoiding a per-flush count() query on the hot path."""

    def _patched_flush_ctx(self, session):
        db_mock = AsyncMock()
        db_mock.query = AsyncMock()
        # Stack the patches; the returned context manager wraps all of them.
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("collab.flush_pipeline.get_db", return_value=db_mock))
        stack.enter_context(EnqueueRecorder.active())
        stack.enter_context(patch("collab.flush_pipeline.extract_doc_mentions", return_value=[]))
        return stack

    async def test_flush_skips_compact_below_threshold(self):
        from ydoc_store import COMPACT_MIN_UPDATES
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True
        session._updates_since_compact = COMPACT_MIN_UPDATES - 1

        with patch("ydoc_store.maybe_compact", new_callable=AsyncMock) as mock_compact:
            with self._patched_flush_ctx(session):
                await session.flush_to_db()
        mock_compact.assert_not_called()

    async def test_flush_compacts_at_threshold_and_resets_counter(self):
        from ydoc_store import COMPACT_MIN_UPDATES
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True
        session._updates_since_compact = COMPACT_MIN_UPDATES

        with patch("ydoc_store.maybe_compact", new_callable=AsyncMock) as mock_compact:
            with self._patched_flush_ctx(session):
                await session.flush_to_db()
        mock_compact.assert_awaited_once_with("d1")
        assert session._updates_since_compact == 0


class TestBacklinksEmitTimeout:
    """T1: when the backlinks_changed_batch emit times out, the mention-id cache
    must NOT advance — otherwise the (idempotent) rebuild + re-emit never re-runs
    and peers miss that backlinks delta forever. The cache staying stale makes the
    next flush retry the whole mention delta."""

    async def test_emit_timeout_leaves_cache_stale_for_retry(self):
        session = _mk_session()
        session._last_mention_ids = {"old1"}
        db_mock = AsyncMock()
        content = "body [[doc:changed]]"

        with patch("collab.flush_pipeline.extract_doc_mentions", return_value=["changed"]):
            with patch("mentions.rebuild_doc_mentions", new_callable=AsyncMock):
                with patch("event_bus.emit", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
                    await session._flush_pipeline._rebuild_mentions_if_needed(db_mock, content)

        # Cache unchanged so the next flush sees the same delta and retries.
        assert session._last_mention_ids == {"old1"}


class TestFlushPipelineProxySurface:
    """W7 extracted FlushPipeline out of CollabSession. Callers that touch
    flush-private state through the session surface (events.py sets the baseline
    tables cache on restore/agent-wholesale-edit) must keep reaching the pipeline —
    the value the flush reads as the content-loss backup baseline (INVARIANT #6).
    A missing write-through proxy leaves the pipeline baseline silently stale."""

    def test_last_flushed_tables_json_writes_through_to_pipeline(self):
        session = _mk_session()
        assert session._flush_pipeline._last_flushed_tables_json == "{}"  # pipeline default

        baseline = '{"tables":{"t1":{"rows":1}}}'
        session._last_flushed_tables_json = baseline  # the events.py restore path

        assert session._flush_pipeline._last_flushed_tables_json == baseline
        assert session._last_flushed_tables_json == baseline  # read-back parity
