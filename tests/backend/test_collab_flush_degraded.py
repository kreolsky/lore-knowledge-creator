"""Tests for flush degradation signalling — PR-2 (R2).

Covers: CollabSession tracks consecutive flush failures; broadcasts
save_degraded on 3 consecutive failures; broadcasts save_recovered
on the first successful flush after degradation. Threshold prevents
flapping on single transient errors.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from collab.registry import _sessions
from collab.session import CollabSession, ConnectedClient
from emit_recorder import EmitRecorder
from enqueue_recorder import EnqueueRecorder
from pycrdt import Doc, Text

DEGRADED_THRESHOLD = 3


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


class TestFlushDegraded:
    async def test_no_broadcast_before_threshold(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD - 1):
                await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        assert not any(m.get("type") == "save_degraded" for m in sent)

    async def test_broadcast_on_threshold(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD):
                await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        degraded_msgs = [m for m in sent if m.get("type") == "save_degraded"]
        assert len(degraded_msgs) == 1
        assert degraded_msgs[0]["entity_id"] == "d1"

    async def test_no_double_broadcast_on_additional_failures(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD + 3):
                await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        degraded_msgs = [m for m in sent if m.get("type") == "save_degraded"]
        assert len(degraded_msgs) == 1

    async def test_recovery_broadcast_on_successful_flush(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        db_mock = AsyncMock()
        db_mock.query = AsyncMock()

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD):
                await session.flush_to_db()

        client.ws.send_text.reset_mock()
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", return_value=db_mock):
            with EnqueueRecorder.active():
                with patch("collab.flush_pipeline.extract_doc_mentions", return_value=[]):
                    await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        recovered_msgs = [m for m in sent if m.get("type") == "save_recovered"]
        assert len(recovered_msgs) == 1
        assert recovered_msgs[0]["entity_id"] == "d1"

    async def test_no_recovery_broadcast_when_not_degraded(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        db_mock = AsyncMock()
        db_mock.query = AsyncMock()

        with patch("collab.flush_pipeline.get_db", return_value=db_mock):
            with EnqueueRecorder.active():
                with patch("collab.flush_pipeline.extract_doc_mentions", return_value=[]):
                    await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        recovered_msgs = [m for m in sent if m.get("type") == "save_recovered"]
        assert len(recovered_msgs) == 0

    async def test_counter_resets_on_recovery(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        db_mock = AsyncMock()
        db_mock.query = AsyncMock()

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD):
                await session.flush_to_db()

        session._dirty = True
        with patch("collab.flush_pipeline.get_db", return_value=db_mock):
            with EnqueueRecorder.active():
                with patch("collab.flush_pipeline.extract_doc_mentions", return_value=[]):
                    await session.flush_to_db()

        client.ws.send_text.reset_mock()

        session._dirty = True
        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD - 1):
                await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        assert not any(m.get("type") == "save_degraded" for m in sent)

    async def test_no_broadcast_when_content_unchanged(self):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._last_flushed_content = "hello"
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        assert not any(m.get("type") == "save_degraded" for m in sent)


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

    async def test_emit_success_advances_cache(self):
        session = _mk_session()
        session._last_mention_ids = {"old1"}
        db_mock = AsyncMock()
        content = "body [[doc:changed]]"

        with patch("collab.flush_pipeline.extract_doc_mentions", return_value=["changed"]):
            with patch("mentions.rebuild_doc_mentions", new_callable=AsyncMock):
                with EmitRecorder.active():
                    await session._flush_pipeline._rebuild_mentions_if_needed(db_mock, content)

        assert session._last_mention_ids == {"changed"}


class TestFlushFailureTelemetry:
    """Telemetry observability for the incident-class flush-failure site (plan 1.1).

    A failed flush_to_db must emit a `collab/flush_failure` telemetry row (sibling to
    server-reap at session.py) so the NONE-coerce-on-UPDATE silent-write class is
    queryable in the telemetry channel, not just in logs + last_save_failed_at.
    Fire-and-forget per telemetry_store INVARIANT: a telemetry failure must never
    break or delay the flush path.
    """

    @pytest.fixture(autouse=True)
    def _capture_telemetry(self, monkeypatch):
        recorded = []

        async def _record(events):
            recorded.extend(events)

        monkeypatch.setattr("telemetry_store.record_telemetry_events", _record)
        return recorded

    async def test_flush_failure_emits_telemetry_row(self, _capture_telemetry):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            await session.flush_to_db()

        assert any(
            e.get("kind") == "flush_failure" and e.get("entity_id") == "d1"
            for e in _capture_telemetry
        )

    async def test_telemetry_failure_never_breaks_flush(self, monkeypatch):
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client
        session._dirty = True

        async def _boom(events):
            raise RuntimeError("telemetry down")

        monkeypatch.setattr("telemetry_store.record_telemetry_events", _boom)

        with patch("collab.flush_pipeline.get_db", side_effect=RuntimeError("DB down")):
            for _ in range(DEGRADED_THRESHOLD):
                await session.flush_to_db()

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        assert any(m.get("type") == "save_degraded" for m in sent)


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

    def test_last_flushed_content_and_mentions_still_proxied(self):
        # Regression guard: the three pre-existing proxies keep working after adding
        # the tables proxy, so the restore path's _last_flushed_content write-through
        # (events.py) is not disturbed.
        session = _mk_session()
        session._last_flushed_content = "restored body"
        session._last_mention_ids = {"d9"}
        session._updates_since_compact = 7
        assert session._flush_pipeline._last_flushed_content == "restored body"
        assert session._flush_pipeline._last_mention_ids == {"d9"}
        assert session._flush_pipeline._updates_since_compact == 7
