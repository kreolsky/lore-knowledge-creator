"""Tests for the persistent project-level collab WebSocket (join/leave multiplexing).

Covers the multiplexed control plane: connect/auth, per-entity join/leave,
multi-entity routing, document switching, user_joined/user_left broadcast, and
flush-on-leave persistence.

CRDT note: document content travels as binary Yjs sync frames (covered by the
pycrdt + backplane convergence tests and by handle_binary_message in
test_collab_persistence). These tests exercise the JSON control plane and drive
flush content through the session object — they do not reconstruct binary frames.
"""

import json

import pytest
from collab.registry import _session_key, _sessions
from helpers import make_token, set_session_text

# ─── 1. Connection & Auth ─────────────────────────────────────────────────────


class TestProjectWsConnection:
    def test_connect_authenticated(self, sync_app, collab_project):
        pid, _, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat"}))  # no crash = alive

    def test_heartbeat_echoes_t_for_rtt(self, sync_app, collab_project):
        # WHY: the multiplexed channel must echo heartbeat_ack with the client `t`
        # so the editor can measure per-beat RTT (perf telemetry). A bare `pass`
        # left the `rtt` telemetry kind permanently empty.
        pid, _, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "heartbeat", "t": 123456}))
            reply = json.loads(ws.receive_text())
        assert reply["type"] == "heartbeat_ack"
        assert reply["t"] == 123456

    def test_clean_disconnect_logs_no_error(self, sync_app, collab_project, caplog):
        # Regression: ws.receive() returns the "websocket.disconnect" frame; before the
        # fix the loop fell through and the next receive() raised RuntimeError, logged
        # at ERROR on every normal disconnect. A clean close must produce no ERROR.
        import logging

        pid, _, admin_token, *_ = collab_project
        with caplog.at_level(logging.ERROR, logger="routes.collab_project_ws"):
            with sync_app.websocket_connect(
                f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
            ) as ws:
                ws.send_text(json.dumps({"type": "heartbeat"}))
        assert "Project collab WS error" not in caplog.text

    def test_connect_unauthenticated(self, sync_app, collab_project):
        pid, *_ = collab_project
        with pytest.raises(Exception):
            with sync_app.websocket_connect(f"/ws/collab/project/{pid}") as ws:
                ws.receive_text()

    def test_connect_no_project_access(self, sync_app, collab_project):
        pid, *_ = collab_project
        outsider_token = make_token("outsider-001", "outsider", "user")
        with pytest.raises(Exception):
            with sync_app.websocket_connect(
                f"/ws/collab/project/{pid}", cookies={"lore_session": outsider_token}
            ) as ws:
                ws.receive_text()


# ─── 2. Join / Leave Protocol ─────────────────────────────────────────────────


class TestJoinLeave:
    def test_join_document(self, sync_app, collab_project):
        """Joining a document returns an init message keyed by entity_id."""
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "init"
            assert msg["entity_id"] == doc_id
            assert isinstance(msg["users"], list)

    def test_join_invalid_entity(self, sync_app, collab_project):
        """Joining a non-existent entity returns an error; connection stays open."""
        pid, _, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({
                "type": "join", "entity_type": "doc", "entity_id": "nonexistent-doc-999",
            }))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
            ws.send_text(json.dumps({"type": "heartbeat"}))  # still alive

    def test_leave_broadcasts_user_left(self, sync_app, collab_project):
        """When A leaves, peer B on the same entity receives user_left with entity_id."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws_a, sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
        ) as ws_b:
            ws_a.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws_a.receive_text())  # init
            ws_b.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws_b.receive_text())  # init
            json.loads(ws_a.receive_text())  # user_joined (B)

            ws_b.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))
            msg = json.loads(ws_a.receive_text())
            assert msg["type"] == "user_left"
            assert msg["entity_id"] == doc_id

    def test_rejoin_after_leave(self, sync_app, collab_project):
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            assert json.loads(ws.receive_text())["type"] == "init"
            ws.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            init2 = json.loads(ws.receive_text())
            assert init2["type"] == "init"
            assert init2["entity_id"] == doc_id


# ─── 3. Multi-Entity Multiplexing ─────────────────────────────────────────────


class TestMultiEntity:
    def test_join_two_entities(self, sync_app, collab_ref_project):
        """Can join doc and ref simultaneously on one connection."""
        pid, doc_id, ref_id, admin_token, *_ = collab_ref_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            assert json.loads(ws.receive_text())["entity_id"] == doc_id
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": ref_id}))
            assert json.loads(ws.receive_text())["entity_id"] == ref_id

    def test_leave_one_keep_other(self, sync_app, collab_ref_project):
        """Leaving one entity doesn't affect the other (flush of the kept one still acks)."""
        pid, doc_id, ref_id, admin_token, *_ = collab_ref_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws.receive_text())
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": ref_id}))
            json.loads(ws.receive_text())

            ws.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))

            # ref still joined → flush acks for it
            ws.send_text(json.dumps({"type": "flush", "entity_id": ref_id}))
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "flush_ack"
            assert ack["entity_id"] == ref_id


# ─── 4. Document Switch (Leave + Join) ────────────────────────────────────────


class TestDocumentSwitch:
    @pytest.mark.asyncio
    async def test_switch_document(self, sync_app, client, collab_project):
        """Switch from one doc to another without reconnecting."""
        pid, doc_id, admin_token, *_ = collab_project
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Second Doc", "content": "Second content"},
            cookies={"lore_session": admin_token},
        )
        doc2_id = resp.json()["document_id"]

        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            assert json.loads(ws.receive_text())["entity_id"] == doc_id

            ws.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc2_id}))
            init2 = json.loads(ws.receive_text())
            assert init2["type"] == "init"
            assert init2["entity_id"] == doc2_id


# ─── 5. Multi-Client Broadcast routing ────────────────────────────────────────


class TestMultiClientBroadcast:
    def test_user_joined_broadcast_to_peer(self, sync_app, collab_project):
        """When B joins a doc A is on, A receives user_joined carrying entity_id."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws_a, sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
        ) as ws_b:
            ws_a.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws_a.receive_text())  # init
            ws_b.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws_b.receive_text())  # init

            msg = json.loads(ws_a.receive_text())
            assert msg["type"] == "user_joined"
            assert msg["entity_id"] == doc_id


# ─── 6. Flush on Leave ────────────────────────────────────────────────────────


class TestFlushOnLeave:
    @pytest.mark.asyncio
    async def test_flush_on_leave(self, sync_app, client, collab_project):
        """After an edit + explicit flush + leave, content is persisted to DB."""
        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))
            json.loads(ws.receive_text())  # init

            # Drive the edit through the session (the binary sync path is covered
            # elsewhere); the flush control message persists derived content.
            session = _sessions[_session_key("doc", doc_id)]
            set_session_text(session, "Flushed content")
            session._dirty = True

            ws.send_text(json.dumps({"type": "flush", "entity_id": doc_id}))
            flush_ack = json.loads(ws.receive_text())
            assert flush_ack["type"] == "flush_ack"

            ws.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))

        resp = await client.get(f"/api/documents/{doc_id}", cookies={"lore_session": admin_token})
        data = resp.json()
        assert resp.status_code == 200, f"GET doc failed: {data}"
        assert "Flushed content" in data["content"]


# ─── 7. Auth Per Join ─────────────────────────────────────────────────────────


class TestAuthPerJoin:
    @pytest.mark.asyncio
    async def test_join_entity_from_wrong_project(self, sync_app, client, collab_project, collab_ref_project):
        """Cannot join an entity that belongs to a different project."""
        pid1, _, admin_token, *_ = collab_project
        _, _, ref_id, *_ = collab_ref_project

        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid1}", cookies={"lore_session": admin_token}
        ) as ws:
            ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": ref_id}))
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"

