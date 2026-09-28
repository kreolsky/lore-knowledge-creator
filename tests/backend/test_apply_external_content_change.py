"""Tests for apply_external_content_change no-op guard and flush_ack entity_id.

CRDT migration: content is a read-only property derived from the pycrdt Y.Doc.
Tests mutate the Y.Text directly instead of setting session.content.
"""

import json

import pytest
from helpers import join_collab_ws, project_collab_url


@pytest.fixture
def _clear_sessions():
    from collab.registry import _sessions
    _sessions.clear()
    yield
    for session in _sessions.values():
        session.stop_periodic_flush()
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
    _sessions.clear()


def _set_session_text(session, text: str) -> None:
    t = session._get_text()
    old_len = len(t)
    if old_len > 0:
        del t[0:old_len]
    if text:
        t += text


class TestApplyExternalContentChangeNoop:

    def test_noop_when_content_matches(self, sync_app, collab_project, _clear_sessions):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "identical")
            session._dirty = False
            import asyncio
            loop = asyncio.get_event_loop()
            result = loop.run_until_complete(
                apply_external_content_change("doc", doc_id, "identical")
            )
            assert result is True
            assert session._dirty is False

    def test_noop_already_persisted_clears_dirty(self, sync_app, collab_project, _clear_sessions):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "xyz")
            session._dirty = True
            import asyncio
            loop = asyncio.get_event_loop()
            loop.run_until_complete(
                apply_external_content_change("doc", doc_id, "xyz", already_persisted=True)
            )
            assert session._dirty is False

    def test_different_content_still_works(self, sync_app, collab_project, _clear_sessions):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "old")
            session._dirty = False
            import asyncio
            loop = asyncio.get_event_loop()
            loop.run_until_complete(
                apply_external_content_change("doc", doc_id, "new")
            )
            assert session.content == "new"
            assert session._dirty is True


class TestFlushAckIncludesEntityId:

    def test_flush_ack_has_entity_id(self, sync_app, collab_project):
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            ws.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "flush_ack"
            assert ack["entity_id"] == doc_id
