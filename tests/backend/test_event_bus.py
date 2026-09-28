"""Tests for the in-process event bus — unit tests + collab integration.

TDD: written before event_bus.py implementation. Defines the contract:
- on(event, callback) subscribes, off(event, callback) unsubscribes
- emit(event, **kwargs) calls all subscribers, async-safe, fire-and-forget
- Collab routes events from REST handlers to WS clients via bus
"""

import asyncio
import json

import pytest
from helpers import join_collab_ws, project_collab_url


async def _drain():
    """Yield to the loop so fire-and-forget `create_task` callbacks scheduled by
    event_bus.emit() get a chance to run before assertions. Two yields cover the
    common case: one to start the task, one to let it complete past its first
    `await` point (relevant for async handlers)."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)

# ─── 1. Unit Tests ──────────────────────────────────────────────────────────


class TestEventBusUnit:
    """Unit tests for the event bus module."""

    @pytest.mark.asyncio
    async def test_emit_calls_subscriber(self):
        """Emitting an event calls registered sync subscriber with kwargs."""
        from event_bus import emit, off, on

        calls = []
        def handler(**kw):
            calls.append(kw)

        on("test_event", handler)
        try:
            await emit("test_event", foo="bar")
            await _drain()
            assert len(calls) == 1
            assert calls[0] == {"foo": "bar"}
        finally:
            off("test_event", handler)

    @pytest.mark.asyncio
    async def test_emit_no_subscribers_no_error(self):
        """Emitting with no subscribers does not raise."""
        from event_bus import emit

        await emit("nonexistent_event", data="ignored")

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        """Multiple subscribers for the same event all get called."""
        from event_bus import emit, off, on

        results = []
        def handler_a(**kw):
            results.append("a")
        def handler_b(**kw):
            results.append("b")

        on("multi", handler_a)
        on("multi", handler_b)
        try:
            await emit("multi", x=1)
            await _drain()
            assert sorted(results) == ["a", "b"]
        finally:
            off("multi", handler_a)
            off("multi", handler_b)

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        """After off(), the callback is no longer called."""
        from event_bus import emit, off, on

        calls = []
        def handler(**kw):
            calls.append(1)

        on("unsub_test", handler)
        off("unsub_test", handler)
        await emit("unsub_test")
        assert calls == []

    @pytest.mark.asyncio
    async def test_async_subscriber(self):
        """Async subscribers are awaited correctly."""
        from event_bus import emit, off, on

        calls = []
        async def async_handler(**kw):
            calls.append(kw)

        on("async_test", async_handler)
        try:
            await emit("async_test", val=42)
            await _drain()
            assert calls == [{"val": 42}]
        finally:
            off("async_test", async_handler)

    @pytest.mark.asyncio
    async def test_subscriber_error_does_not_break_others(self):
        """If one subscriber raises, others still run."""
        from event_bus import emit, off, on

        calls = []
        def bad_handler(**kw):
            raise ValueError("boom")
        def good_handler(**kw):
            calls.append("ok")

        on("error_test", bad_handler)
        on("error_test", good_handler)
        try:
            await emit("error_test")
            await _drain()
            assert calls == ["ok"]
        finally:
            off("error_test", bad_handler)
            off("error_test", good_handler)

    @pytest.mark.asyncio
    async def test_emit_publishes_to_backplane_without_local_subscribers(self, monkeypatch):
        """emit() must publish to the backplane even when there are NO local subscribers.

        Regression (BUG B): the arq worker process has an empty _subscribers, but the web
        replica subscribes to evt:{type} for cross-process fan-out (worker
        transcription_complete → web extractor hook). An early return on empty subscribers
        silently broke the entire worker→web bridge. Also guards the GC regression: the
        publish runs via a strong-ref'd background task, not a fire-and-forget create_task.
        """
        import backplane

        import event_bus

        published = []

        class _FakeBackplane:
            async def publish(self, channel, data):
                published.append((channel, data))

        monkeypatch.setattr(backplane, "get_backplane", lambda: _FakeBackplane())

        assert not event_bus._subscribers.get("orphan_evt")  # no local subscriber
        await event_bus.emit("orphan_evt", reference_id="X", project_id="p")
        await _drain()

        assert len(published) == 1
        channel, _ = published[0]
        assert channel == "evt:orphan_evt"

    @pytest.mark.asyncio
    async def test_backplane_publish_failure_increments_counter(self, monkeypatch):
        """A failed backplane publish must surface — increments the counter that
        /api/health exposes. Regression guard for the silent Redis SPOF: before this,
        publish failures were logged at DEBUG only, hiding cross-process divergence.
        """
        import backplane

        import event_bus

        class _BrokenBackplane:
            async def publish(self, channel, data):
                raise RuntimeError("redis down")

        monkeypatch.setattr(backplane, "get_backplane", lambda: _BrokenBackplane())

        before = event_bus.backplane_publish_failures()
        await event_bus.emit("evt_with_broken_redis", reference_id="X")
        await _drain()

        assert event_bus.backplane_publish_failures() == before + 1


# ─── 2. Collab Integration ──────────────────────────────────────────────────


class TestCollabEventRouting:
    """Integration: events emitted from REST handlers reach WS clients via bus."""

    # Note: test_note_created_via_bus removed — notes are now chat_sessions,
    # note creation events tested via chat session routes.

    @pytest.mark.asyncio
    async def test_entity_deleted_via_bus(self, sync_app, client, collab_project):
        """REST deletes doc → bus routes → WS client receives doc_deleted."""
        pid, doc_id, admin_token, _, admin_uid, _ = collab_project
        # Create a second doc to delete (don't delete the only doc)
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Deletable", "content": "bye"},
            cookies={"lore_session": admin_token},
        )
        del_id = resp.json()["document_id"]
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, del_id)
            json.loads(ws.receive_text())  # init (join ack)
            resp = await client.delete(
                f"/api/documents/{del_id}",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "doc_deleted"

    @pytest.mark.asyncio
    async def test_checkpoint_created_via_bus(self, sync_app, client, collab_project):
        """REST creates checkpoint → bus routes → WS client receives checkpoint_created."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": user_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            json.loads(ws.receive_text())  # init (join ack)
            resp = await client.post(
                "/api/checkpoints",
                json={"document_id": doc_id, "label": "v1", "comment": "First snapshot"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            cp_id = resp.json()["checkpoint_id"]
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "checkpoint_created"
            assert msg["checkpoint"]["checkpoint_id"] == cp_id
            assert msg["checkpoint"]["label"] == "v1"

    @pytest.mark.asyncio
    async def test_backlinks_changed_via_bus(self, sync_app, client, collab_project):
        """Content with mention link saved → bus emits backlinks_changed
        → WS client on target doc receives event."""
        pid, doc_id, admin_token, _, admin_uid, _ = collab_project
        # Create a second doc that will be mentioned
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Target Doc", "content": ""},
            cookies={"lore_session": admin_token},
        )
        target_id = resp.json()["document_id"]
        # Connect WS, join the target doc
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, target_id)
            json.loads(ws.receive_text())  # init (join ack)
            # PATCH source doc with mention of target
            resp = await client.patch(
                f"/api/documents/{doc_id}",
                json={"content": f"Link to [target]({target_id})"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "backlinks_changed"
