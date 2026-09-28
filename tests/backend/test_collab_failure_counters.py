"""Tests for the collab catch-all failure counters (plan 1.1).

Every best-effort `except Exception` site in the collab cluster (flush_pipeline,
session, registry) plus event_bus must increment a monotonic counter exposed on
/api/health — a persistent failure that only logs is invisible to monitoring.
Mirrors the backplane_publish_failures model. The flush_to_db failure site
additionally emits a `flush_failure` telemetry row (see
test_collab_flush_degraded.py::TestFlushFailureTelemetry).
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from collab.registry import _sessions
from collab.session import CollabSession, ConnectedClient
from emit_recorder import EmitRecorder
from pycrdt import Doc, Text


def _mk_session(entity_id="d1") -> CollabSession:
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello"
    doc._lore_text = text
    return CollabSession(entity_type="doc", entity_id=entity_id, ydoc=doc)


@pytest.fixture(autouse=True)
def _clear_sessions():
    _sessions.clear()
    yield
    _sessions.clear()


class TestFlushPipelineCounters:
    """Every best-effort flush-pipeline guard increments flush_pipeline_failures()."""

    async def test_backup_enqueue_failure_increments_counter(self):
        from collab import flush_pipeline as fp

        session = _mk_session()
        db_mock = AsyncMock()
        db_mock.query = AsyncMock()
        with patch("jobs.pool.enqueue", side_effect=RuntimeError("arq down")):
            before = fp.flush_pipeline_failures()
            await session._flush_pipeline._flush_content_to_db(db_mock, "hello")
        assert fp.flush_pipeline_failures() == before + 1

    async def test_compact_failure_increments_counter(self):
        from collab import flush_pipeline as fp

        from ydoc_store import COMPACT_MIN_UPDATES

        session = _mk_session()
        session._updates_since_compact = COMPACT_MIN_UPDATES
        with patch("ydoc_store.maybe_compact", side_effect=RuntimeError("compact boom")):
            with EmitRecorder.active():
                before = fp.flush_pipeline_failures()
                await session._flush_pipeline._handle_flush_success("hello")
        assert fp.flush_pipeline_failures() == before + 1

    async def test_content_flushed_listener_failure_increments_counter(self):
        from collab import flush_pipeline as fp

        session = _mk_session()

        # The AsyncMock side_effect (an exception instance) is raised INSIDE the awaited
        # coroutine via _with_timeout → wait_for, so the SAME "content_flushed listener
        # failed" except-Exception branch fires and no coroutine is left unawaited.
        with patch(
            "event_bus.emit",
            new_callable=AsyncMock,
            side_effect=RuntimeError("listener exploded"),
        ):
            before = fp.flush_pipeline_failures()
            await session._flush_pipeline._handle_flush_success("hello")
        assert fp.flush_pipeline_failures() == before + 1

    async def test_degraded_marker_write_failure_increments_counter(self):
        from collab import flush_pipeline as fp

        session = _mk_session()
        session._flush_pipeline._consecutive_flush_failures = fp.FLUSH_DEGRADED_THRESHOLD - 1
        recorded = []

        async def _record(events):
            recorded.extend(events)

        db_mock = AsyncMock()
        db_mock.query = AsyncMock(side_effect=RuntimeError("db down again"))
        with patch("telemetry_store.record_telemetry_events", new=_record):
            before = fp.flush_pipeline_failures()
            await session._flush_pipeline._handle_flush_failure(db_mock)
        assert fp.flush_pipeline_failures() == before + 1
        assert any(e.get("kind") == "flush_failure" for e in recorded)

    async def test_enqueue_failures_increment_counter(self):
        from collab import flush_pipeline as fp

        session = _mk_session()
        with patch("jobs.pool.enqueue", side_effect=RuntimeError("arq down")):
            before = fp.flush_pipeline_failures()
            await session._flush_pipeline._enqueue_handoff_backup("pre", "u1", "U1", "{}")
            assert fp.flush_pipeline_failures() == before + 1

            before = fp.flush_pipeline_failures()
            await session._flush_pipeline._enqueue_last_session_backup("u1", "U1")
            assert fp.flush_pipeline_failures() == before + 1


class TestCollabSessionCounters:
    """Every best-effort collab-session guard increments collab_session_failures()."""

    async def test_ws_send_failure_increments_counter(self):
        from collab import session as sess

        session = _mk_session()
        ws = AsyncMock()
        ws.send_text = AsyncMock(side_effect=RuntimeError("ws gone"))
        client = ConnectedClient(ws=ws, user_id="u1", user_name="U1", access_level="full")
        session.clients[id(ws)] = client

        before = sess.collab_session_failures()
        await session.broadcast({"type": "ping"})
        assert sess.collab_session_failures() == before + 1

    async def test_periodic_flush_loop_failure_increments_counter(self, monkeypatch):
        from collab import session as sess

        session = _mk_session()

        async def _boom_flush(self):
            raise RuntimeError("flush exploded")

        monkeypatch.setattr(sess.CollabSession, "_flush_if_needed", _boom_flush)
        monkeypatch.setattr(sess, "FLUSH_INTERVAL_SEC", 0)

        before = sess.collab_session_failures()
        session.start_periodic_flush()
        await asyncio.sleep(0.05)
        session.stop_periodic_flush()
        assert sess.collab_session_failures() > before


class TestCollabRegistryCounters:
    """Every best-effort registry guard increments collab_registry_failures()."""

    async def test_load_fallback_increments_counter(self):
        from collab import registry as reg

        with patch("ydoc_store.load", new=AsyncMock(side_effect=RuntimeError("db down"))):
            before = reg.collab_registry_failures()
            doc = await reg._load_ydoc("d1", "fallback content")
        assert reg.collab_registry_failures() == before + 1
        assert doc is not None

    async def test_cleanup_flush_failure_increments_counter(self):
        from collab import registry as reg

        session = _mk_session()
        _sessions[reg._session_key("doc", "d1")] = session

        with patch(
            "collab.session.CollabSession._flush_if_needed",
            new=AsyncMock(side_effect=RuntimeError("flush boom")),
        ):
            before = reg.collab_registry_failures()
            await reg._cleanup_session("doc", "d1")
        assert reg.collab_registry_failures() == before + 1


class TestEventBusCounters:
    """event_bus best-effort guards increment their own counters."""

    async def _drain(self):
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_subscriber_failure_increments_counter(self):
        import event_bus

        def bad_handler(**kw):
            raise RuntimeError("boom")

        event_bus.on("counter_sub_test", bad_handler)
        try:
            before = event_bus.subscriber_failures()
            await event_bus.emit("counter_sub_test", x=1)
            await self._drain()
            assert event_bus.subscriber_failures() == before + 1
        finally:
            event_bus.off("counter_sub_test", bad_handler)

    async def test_backplane_subscription_failure_increments_counter(self, monkeypatch):
        import backplane

        import event_bus

        class _BrokenBackplane:
            async def subscribe(self, channel, handler):
                raise RuntimeError("redis down")

        monkeypatch.setattr(backplane, "get_backplane", lambda: _BrokenBackplane())

        def handler(**kw):
            pass

        event_bus.on("counter_subscribe_test", handler)
        try:
            before = event_bus.backplane_subscription_failures()
            await event_bus.subscribe_backplane_events()
            assert event_bus.backplane_subscription_failures() == before + 1
        finally:
            event_bus.off("counter_subscribe_test", handler)
