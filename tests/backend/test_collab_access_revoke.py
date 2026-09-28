"""Tests for WS close after access revocation.

Covers: _on_access_changed must close the WS
with code 4003 after a 1-second grace period when access is revoked (None).
The grace period lets the client render the "access revoked" banner before
the connection is torn down.

When access is changed (not revoked), the WS must remain open.
"""

import asyncio
import json
from unittest.mock import AsyncMock

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


class TestOnAccessChangedRevocation:
    async def test_ws_closed_with_4003_on_revocation(self):
        session = _mk_session()
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        await asyncio.sleep(1.5)

        client.ws.close.assert_awaited()
        close_calls = client.ws.close.call_args_list
        code_4003_found = any(
            c.kwargs.get("code") == 4003 or (c.args and c.args[0] == 4003)
            for c in close_calls
        )
        assert code_4003_found, f"Expected close with code 4003, got calls: {close_calls}"

    async def test_client_removed_from_session_on_revocation(self):
        session = _mk_session()
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        await asyncio.sleep(1.5)

        assert id(client.ws) not in session.clients

    async def test_access_revoked_message_sent_before_close(self):
        session = _mk_session()
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        sent_messages = [
            json.loads(call.args[0])
            for call in client.ws.send_text.call_args_list
        ]
        assert any(m.get("type") == "access_revoked" for m in sent_messages)

    async def test_ws_not_closed_when_access_changed_not_revoked(self):
        session = _mk_session()
        client = _mk_client(user_id="u-editor", access_level="full")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-editor", "editor")

        await asyncio.sleep(1.5)

        close_calls = [c for c in client.ws.close.call_args_list
                       if c.kwargs.get("code") == 4003 or (c.args and c.args[0] == 4003)]
        assert len(close_calls) == 0
        assert id(client.ws) in session.clients

    async def test_grace_period_before_close(self):
        session = _mk_session()
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        await asyncio.sleep(0.3)
        code_4003_found = any(
            c.kwargs.get("code") == 4003 or (c.args and c.args[0] == 4003)
            for c in client.ws.close.call_args_list
        )
        assert not code_4003_found, "WS should not be closed before 1s grace period"
