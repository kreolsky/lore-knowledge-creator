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
