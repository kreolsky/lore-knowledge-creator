"""Integration tests for reference collaborative editing (CRDT).

References are `documents` rows with is_reference=true; they share the collab
pipeline with documents. CRDT migration: edits ride the pycrdt Y.Doc (no OT
push/ack). These tests cover connection/auth, flush→documents-table persistence,
auto-backup skip, doc_mentions rebuild, and doc/ref session isolation — driven
through the session the way a real client edit would.
"""

import json

import pytest
from collab.registry import _get_or_create_session, _session_key
from helpers import join_collab_ws, make_token, project_collab_url, set_session_text

from db import get_db

# Fixtures collab_ref_project and sync_app provided by conftest.py


async def _ref_session(ref_id: str):
    """Create the collab session for a reference from its real documents row."""
    db = await get_db()
    rows = await db.query("SELECT * FROM type::record('documents', $id)", {"id": ref_id})
    entity = rows[0]
    return await _get_or_create_session("doc", ref_id, entity.get("content") or "", entity=entity)


async def _ref_content(client, ref_id: str, admin_token: str) -> dict:
    resp = await client.get(f"/api/documents/{ref_id}", cookies={"lore_session": admin_token})
    return resp.json()


# ─── 1. Connection Tests ──────────────────────────────────────────────────


class TestRefWsConnection:
    """Project WS join for a reference: connect, auth, join ack."""

    def test_connect_ref_authenticated(self, sync_app, collab_ref_project):
        """Authenticated user joins the ref on /ws/collab/project/{pid}, gets init with users."""
        pid, _, ref_id, admin_token, *_ = collab_ref_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, ref_id)
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "init"
            assert msg["entity_id"] == ref_id
            assert isinstance(msg["users"], list)

    def test_connect_ref_unauthenticated(self, sync_app, collab_ref_project):
        """Connection without token is rejected."""
        pid, *_ = collab_ref_project
        with pytest.raises(Exception):
            with sync_app.websocket_connect(project_collab_url(pid)) as ws:
                ws.receive_text()

    def test_join_ref_not_found(self, sync_app, collab_ref_project):
        """Joining a nonexistent ref returns an error frame; connection stays open.

        (The per-entity channel closed the socket with 4004 — the join-ack shape
        of the one remaining channel reports the same miss as an error message.)"""
        pid, _, _, admin_token, *_ = collab_ref_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, "nonexistent-ref")
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            assert msg["message"] == "Entity not found"
            ws.send_text(json.dumps({"type": "heartbeat"}))  # still alive

    def test_connect_ref_no_project_access(self, sync_app, collab_ref_project):
        """User without project access cannot connect to ref collab."""
        pid, *_ = collab_ref_project
        outsider_token = make_token("outsider-001", "outsider", "user")
        with pytest.raises(Exception):
            with sync_app.websocket_connect(
                project_collab_url(pid), cookies={"lore_session": outsider_token}
            ) as ws:
                ws.receive_text()


# ─── 2. Persistence Tests ────────────────────────────────────────────────


class TestRefPersistence:
    """Verify ref collab changes flush to the documents table (is_reference=true)."""

    @pytest.mark.asyncio
    async def test_ref_flush_persists_to_documents_table(self, client, collab_ref_project, _clear_sessions):
        _, _, ref_id, admin_token, *_ = collab_ref_project
        session = await _ref_session(ref_id)
        set_session_text(session, "Persisted reference text")
        session._dirty = True
        await session.flush_to_db()

        ref = await _ref_content(client, ref_id, admin_token)
        assert ref.get("is_reference") is True
        assert "Persisted" in ref["content"]

    @pytest.mark.asyncio
    async def test_ref_flush_skips_auto_backup(self, client, collab_ref_project, _clear_sessions):
        """Refs don't trigger auto-backup checkpoints (only documents do)."""
        _, _, ref_id, admin_token, *_ = collab_ref_project
        session = await _ref_session(ref_id)
        set_session_text(session, "x" * 600)
        session._dirty = True
        await session.flush_to_db()

        resp = await client.get(
            f"/api/checkpoints?document_id={ref_id}", cookies={"lore_session": admin_token}
        )
        if resp.status_code == 200:
            assert len(resp.json()) == 0

    @pytest.mark.asyncio
    async def test_ref_flush_rebuilds_doc_mentions(self, client, collab_ref_project, _clear_sessions):
        """Ref flush rebuilds doc_mentions edges (ref→document) for markdown links."""
        _, doc_id, ref_id, admin_token, *_ = collab_ref_project
        session = await _ref_session(ref_id)
        set_session_text(session, f"See [Parent Doc]({doc_id})")
        session._dirty = True
        await session.flush_to_db()

        resp = await client.get(
            f"/api/documents/{doc_id}/backlinks", cookies={"lore_session": admin_token}
        )
        assert resp.status_code == 200
        backlinks = resp.json()["backlinks"]
        assert len(backlinks) >= 1


# ─── 3. Session Isolation Tests ───────────────────────────────────────────


class TestRefDocSessionIsolation:
    """Reference and document collab sessions are fully independent."""

    @pytest.mark.asyncio
    async def test_ref_and_doc_sessions_independent(self, client, collab_ref_project, _clear_sessions):
        """Edits to the ref session don't leak into the doc session and vice versa."""
        _, doc_id, ref_id, admin_token, *_ = collab_ref_project
        db = await get_db()
        doc_row = (await db.query("SELECT * FROM type::record('documents', $id)", {"id": doc_id}))[0]
        doc_session = await _get_or_create_session("doc", doc_id, doc_row.get("content") or "", entity=doc_row)
        ref_session = await _ref_session(ref_id)

        set_session_text(doc_session, "DocOnly content")
        set_session_text(ref_session, "RefOnly content")

        assert doc_session.content == "DocOnly content"
        assert ref_session.content == "RefOnly content"
        assert _session_key("doc", doc_id) != _session_key("doc", ref_id)


# ─── 4. Validation Tests ─────────────────────────────────────────────────


class TestInvalidEntityType:
    """Entity type validation on the project WS join."""

    def test_join_invalid_entity_type(self, sync_app, collab_ref_project):
        """Joining with entity_type other than 'doc' returns an error; connection stays open.

        (The per-entity channel closed the socket with 4002 on the URL-borne
        type — the join shape of the one remaining channel reports the same
        rejection as an error message.)"""
        pid, _, _, admin_token, *_ = collab_ref_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, "some-id", entity_type="invalid")
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            assert msg["message"] == "Invalid entity type"
            ws.send_text(json.dumps({"type": "heartbeat"}))  # still alive
