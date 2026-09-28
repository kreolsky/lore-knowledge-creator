"""Fault-tolerant collab tests — Wave 1: malformed JSON, batch serialization, access helper.

TDD: written BEFORE implementation. These tests PIN the bugs described in the
fault-tolerant plan and will FAIL until fixes are applied.

Task 4a: Malformed JSON on the WS endpoint surfaces an error reply.
Task 7: Event bus subscriber errors log at WARNING level.
Task 8: Non-serializable broadcast payload does NOT kill the batch task.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from collab.registry import _sessions
from collab.session import CollabSession, ConnectedClient
from pycrdt import Doc, Text


def _mk_session(content="Hello world", entity_id="d1", **kw) -> CollabSession:
    # CRDT: content is derived from the Y.Doc — seed it into the doc.
    doc = Doc()
    text = doc.get("content", type=Text)
    if content:
        text += content
    doc._lore_text = text
    return CollabSession(entity_type="doc", entity_id=entity_id, ydoc=doc, **kw)


def _mk_client(user_id="u1", user_name="User1", access_level="full") -> ConnectedClient:
    ws = AsyncMock()
    ws.close = AsyncMock()
    client = ConnectedClient(ws=ws, user_id=user_id, user_name=user_name, access_level=access_level)
    return client


@pytest.fixture(autouse=True)
def _clear_sessions():
    _sessions.clear()
    yield
    for s in list(_sessions.values()):
        try:
            s.stop_periodic_flush()
        except RuntimeError:
            pass
        if s._batch_task and not s._batch_task.done():
            try:
                s._batch_task.cancel()
            except RuntimeError:
                pass
    _sessions.clear()


# ─── Task 4a: Malformed JSON error reply (project WS) ───────────────────────
# (The per-entity WS twin of this test died with the per-entity route in plan
# fewer-layers: it was byte-identical to this one once the channel was gone.)


class TestMalformedJsonProjectWS:
    async def test_malformed_json_returns_error_reply(self, sync_app, collab_project):
        """Sending non-JSON on project WS returns an error message, not silent drop."""
        pid, doc_id, admin_token, _, _, _ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text("not valid json {{{")
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            assert "malformed" in msg.get("code", "").lower() or "malformed" in msg.get("message", "").lower()


# ─── Task 7: Event bus subscriber error visibility ───────────────────────────


class TestEventBusSubscriberErrorVisibility:
    async def test_throwing_subscriber_logs_warning(self, caplog):
        """A failing event bus subscriber logs WARNING + ERROR detail."""
        from event_bus import emit, off, on

        def bad_handler(**kw):
            raise ValueError("subscriber boom")

        on("visibility_test", bad_handler)
        try:
            with caplog.at_level(logging.WARNING, logger="event_bus"):
                await emit("visibility_test", data="x")
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warning_records) > 0, "Expected WARNING log for subscriber error"
        finally:
            off("visibility_test", bad_handler)


# ─── Task 8: _flush_batch serialization guard ────────────────────────────────


class TestFlushBatchSerializationGuard:
    async def test_non_serializable_payload_does_not_kill_batch_task(self):
        """A non-serializable broadcast message is skipped, others still delivered."""
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client

        class BadObj:
            pass

        session._pending_broadcasts.append(({"type": "update", "bad": BadObj()}, set()))
        session._pending_broadcasts.append(({"type": "update", "version": 1, "data": "ok"}, set()))

        await session._flush_batch()

        sent_texts = [c.args[0] for c in client.ws.send_text.call_args_list]
        good_msgs = [json.loads(t) for t in sent_texts if t]
        assert any(m.get("data") == "ok" for m in good_msgs), "Good message was dropped along with bad one"

    async def test_non_serializable_payload_logs_warning(self, caplog):
        """Non-serializable payload is logged at WARNING."""
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client

        class BadObj:
            pass

        session._pending_broadcasts.append(({"type": "update", "bad": BadObj()}, set()))

        with caplog.at_level(logging.WARNING, logger="collab.session"):
            await session._flush_batch()

        assert any("non-serializable" in r.message.lower() or "dropping" in r.message.lower()
                    for r in caplog.records)

    async def test_batch_task_survives_non_serializable(self):
        """The batch task does not crash — subsequent broadcasts still work."""
        session = _mk_session()
        client = _mk_client()
        session.clients[id(client.ws)] = client

        class BadObj:
            pass

        session._pending_broadcasts.append(({"bad": BadObj()}, set()))
        await session._flush_batch()

        session.queue_broadcast({"type": "update", "version": 2})
        assert session._batch_task is not None or session._pending_broadcasts
        client.ws.send_text.reset_mock()
        if session._batch_task:
            await session._batch_task

        sent_texts = [c.args[0] for c in client.ws.send_text.call_args_list]
        assert any("update" in t for t in sent_texts if t)
