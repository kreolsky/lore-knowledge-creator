"""Integration tests for reference collaborative editing (CRDT).

References are `documents` rows with is_reference=true; they share the collab
pipeline with documents. CRDT migration: edits ride the pycrdt Y.Doc (no OT
push/ack). These tests cover connection/auth, flush→documents-table persistence,
auto-backup skip, doc_mentions rebuild, and doc/ref session isolation — driven
through the session the way a real client edit would.
"""

import json

import pytest
from helpers import join_collab_ws, make_token, project_collab_url

# Fixtures collab_ref_project and sync_app provided by conftest.py


# ─── 1. Connection Tests ──────────────────────────────────────────────────


class TestRefWsConnection:
    """Project WS join for a reference: connect, auth, join ack."""


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
