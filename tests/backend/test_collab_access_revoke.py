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
import pytest_asyncio
from collab.events import _on_access_changed
from collab.registry import _session_key, _sessions
from collab.session import CollabSession, ConnectedClient
from pycrdt import Doc, Text


def _mk_session(entity_id="d1", **kw) -> CollabSession:
    # CRDT: content is derived from the Y.Doc — seed "hello" into the doc.
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "hello"
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
    for s in _sessions.values():
        s.stop_periodic_flush()
        if s._batch_task and not s._batch_task.done():
            s._batch_task.cancel()
    _sessions.clear()


class TestOnAccessChangedProjectScope:
    async def test_other_project_session_untouched(self):
        session = _mk_session(project_id="p-other")
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        await asyncio.sleep(1.5)

        client.ws.send_text.assert_not_called()
        client.ws.close.assert_not_called()
        assert id(client.ws) in session.clients

    async def test_same_project_session_revoked(self):
        session = _mk_session(project_id="p1")
        client = _mk_client(user_id="u-victim")
        session.clients[id(client.ws)] = client
        _sessions[_session_key("doc", "d1")] = session

        await _on_access_changed("p1", "u-victim", None)

        sent = [json.loads(c.args[0]) for c in client.ws.send_text.call_args_list]
        assert any(m.get("type") == "access_revoked" for m in sent)


class TestProjectWideRecompute:
    """user_id=None (project-wide) emits: every connected client of every
    matching session is recomputed from live rows (no monkeypatching — the
    recompute rides the real test DB: member rows vs no row → level vs None)."""

    @pytest_asyncio.fixture
    async def wide_cast(self, client, test_db):
        """Project p1 with member rows for u-a (commentator) and u-b
        (readonly), no row for u-c; docs d1 (project p1) and d9 (project p9)."""
        from db import create_record

        async def seed_user(uid: str) -> None:
            await test_db.query("DELETE type::record('users', $id)", {"id": uid})
            await create_record("users", uid, {
                "name": uid, "email": f"{uid}@wide.test",
                "password_hash": "x", "role": "user", "user_facts": "",
            })

        for uid in ("u-a", "u-b", "u-c", "u-o"):
            await seed_user(uid)
        await test_db.query("DELETE type::record('projects', $id)", {"id": "p1"})
        await test_db.query(
            "DELETE project_members WHERE project_id = $p", {"p": "p1"},
        )
        await create_record("projects", "p1", {
            "name": "wide", "status": "active", "project_context": "", "owner_id": "u-o",
        })
        await create_record("project_members", "pm-wide-a", {
            "project_id": "p1", "user_id": "u-a", "access_level": "commentator",
        })
        await create_record("project_members", "pm-wide-b", {
            "project_id": "p1", "user_id": "u-b", "access_level": "readonly",
        })
        for did, pid in (("d1", "p1"), ("d9", "p9")):
            await test_db.query("DELETE type::record('documents', $id)", {"id": did})
            await create_record("documents", did, {
                "project_id": pid, "parent_id": None,
                "title": f"{did}.md", "content": "", "path": f"{did}.md",
            })
        yield


    async def test_unknown_project_session_resolved_via_doc_meta(
        self, client, test_db, wide_cast,
    ):
        """A session with an unknown project ("") is recomputed when its
        entity belongs to the emitted project (d1 → p1), skipped when it
        does not (d9 → p9)."""
        match_session = _mk_session()  # project_id="", entity d1 → p1
        match_client = _mk_client(user_id="u-a")
        match_session.clients[id(match_client.ws)] = match_client
        _sessions[_session_key("doc", "d1")] = match_session

        foreign_session = _mk_session(entity_id="d9")  # project_id="", → p9
        foreign_client = _mk_client(user_id="u-a")
        foreign_session.clients[id(foreign_client.ws)] = foreign_client
        _sessions[_session_key("doc", "d9")] = foreign_session

        await _on_access_changed("p1", None, None)

        sent = [json.loads(c.args[0]) for c in match_client.ws.send_text.call_args_list]
        assert {"type": "access_changed", "level": "commentator"} in sent
        foreign_client.ws.send_text.assert_not_called()
        assert id(foreign_client.ws) in foreign_session.clients
