"""Tests for WS send-failure visibility — logger.warning + explicit ws.close(code=1011).

Covers: three bare except-pass sites in collab.py + one in collab_project_ws.py
must log the failure, close the WS with code 1011, and remove the client.
"""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from collab.events import _on_access_changed
from collab.registry import _session_key, _sessions
from collab.session import CollabSession, ConnectedClient
from pycrdt import Doc, Text


def _mk_session(**kw) -> CollabSession:
    # CRDT: content is derived from the Y.Doc — seed "hello" into the doc.
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


class TestFlushBatchSendFailure:
    async def test_client_removed_on_send_failure(self):
        session = _mk_session()
        good_client = _mk_client(user_id="u-good")
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("connection lost")

        session.clients[id(good_client.ws)] = good_client
        session.clients[id(bad_client.ws)] = bad_client

        session.queue_broadcast({"type": "update", "entity_id": "d1", "version": 1, "changes": []})

        with patch.object(session, "_flush_batch", wraps=session._flush_batch):
            await session._flush_batch()

        assert id(bad_client.ws) not in session.clients
        assert id(good_client.ws) in session.clients

    async def test_ws_closed_with_1011_on_send_failure(self):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("connection lost")

        session.clients[id(bad_client.ws)] = bad_client
        session.queue_broadcast({"type": "update", "entity_id": "d1", "version": 1, "changes": []})

        await session._flush_batch()

        bad_client.ws.close.assert_awaited()
        call_args = bad_client.ws.close.call_args
        assert call_args.kwargs.get("code") == 1011 or (call_args.args and call_args.args[0] == 1011)

    async def test_logger_warning_on_send_failure(self, caplog):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("connection lost")

        session.clients[id(bad_client.ws)] = bad_client
        session.queue_broadcast({"type": "update", "entity_id": "d1", "version": 1, "changes": []})

        with caplog.at_level(logging.WARNING, logger="collab.session"):
            await session._flush_batch()

        assert any("WS send failed" in r.message for r in caplog.records)


class TestBroadcastSendFailure:
    async def test_client_removed_on_broadcast_failure(self):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("broken pipe")

        session.clients[id(bad_client.ws)] = bad_client

        await session.broadcast({"type": "user_joined"})

        assert id(bad_client.ws) not in session.clients

    async def test_ws_closed_with_1011_on_broadcast_failure(self):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("broken pipe")

        session.clients[id(bad_client.ws)] = bad_client

        await session.broadcast({"type": "user_joined"})

        bad_client.ws.close.assert_awaited()
        call_args = bad_client.ws.close.call_args
        assert call_args.kwargs.get("code") == 1011 or (call_args.args and call_args.args[0] == 1011)

    async def test_logger_warning_on_broadcast_failure(self, caplog):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("broken pipe")

        session.clients[id(bad_client.ws)] = bad_client

        with caplog.at_level(logging.WARNING, logger="collab.session"):
            await session.broadcast({"type": "user_joined"})

        assert any("WS send failed" in r.message for r in caplog.records)


class TestAccessChangedSendFailure:
    async def test_client_cleaned_up_on_access_changed_failure(self):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("closed")

        session.clients[id(bad_client.ws)] = bad_client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-bad", None)

        assert id(bad_client.ws) not in session.clients

    async def test_ws_closed_with_1011_on_access_changed_failure(self):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("closed")

        session.clients[id(bad_client.ws)] = bad_client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-bad", None)

        bad_client.ws.close.assert_awaited()
        call_args = bad_client.ws.close.call_args
        assert call_args.kwargs.get("code") == 1011 or (call_args.args and call_args.args[0] == 1011)

    async def test_logger_warning_on_access_changed_failure(self, caplog):
        session = _mk_session()
        bad_client = _mk_client(user_id="u-bad")
        bad_client.ws.send_text.side_effect = RuntimeError("closed")

        session.clients[id(bad_client.ws)] = bad_client
        _sessions[_session_key("doc", "d1")] = session

        with caplog.at_level(logging.WARNING, logger="collab.session"):
            await _on_access_changed("p1", "u-bad", None)

        assert any("WS send failed" in r.message for r in caplog.records)
