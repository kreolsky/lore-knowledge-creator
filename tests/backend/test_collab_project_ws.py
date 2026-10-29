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

# ─── 1. Connection & Auth ─────────────────────────────────────────────────────


class TestProjectWsConnection:

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


# ─── 2. Join / Leave Protocol ─────────────────────────────────────────────────


class TestJoinLeave:


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
