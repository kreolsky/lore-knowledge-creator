"""Tests for project-level WebSocket — tree change notifications.

Verifies that REST mutations emit project-scoped events that reach
connected project WS clients.
"""

import json

import pytest


class _FakeProjectClient:
    """A stand-in project-WS socket: records sends, optionally dies."""

    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail

    async def send_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("socket dead")
        self.sent.append(text)


class TestSendToUser:
    """ProjectSession identity + the user-filtered send (plan
    agent-line-harness-lifecycle step 5) — chat frames ride send_to_user,
    never broadcast."""


    @pytest.mark.asyncio
    async def test_send_to_user_drops_dead_sockets(self):
        from routes.project_ws import ProjectSession

        session = ProjectSession("p1")
        dead, alive = _FakeProjectClient(fail=True), _FakeProjectClient()
        session.add(dead, "u1")
        session.add(alive, "u1")

        await session.send_to_user("u1", {"type": "x"})

        assert id(dead) not in session.clients
        assert len(alive.sent) == 1


class TestProjectWs:
    """Integration: project-level events reach WS clients via event bus."""


    @pytest.mark.asyncio
    async def test_reference_deleted_batch_reaches_project_ws(self, sync_app, client, collab_project):
        """POST /api/references/batch-delete → project WS receives documents_deleted_batch."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        ref_ids = []
        for i in range(2):
            resp = await client.post(
                "/api/references",
                json={"project_id": pid, "document_id": doc_id, "title": f"BatchRef{i}", "media_type": "markdown"},
                cookies={"lore_session": admin_token},
            )
            ref_ids.append(resp.json()["reference_id"])
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())
            resp = await client.post(
                "/api/references/batch-delete",
                json={"reference_ids": ref_ids},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            # 4.2: batch-delete now emits a single documents_deleted_batch event
            # instead of per-reference reference_deleted events.
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "documents_deleted_batch"
            assert set(msg["reference_ids"]) == set(ref_ids)
            assert set(msg["document_ids"]) == set(ref_ids)

    @pytest.mark.asyncio
    async def test_reference_deleted_cascade_reaches_project_ws(self, sync_app, client, collab_project):
        """DELETE /api/documents/{id}?delete_children=false (lift mode) with child
        refs → project WS receives reference_deleted per cascade-deleted child ref.
        The default subtree mode emits one documents_deleted_batch instead
        (covered by test_subtree_delete.py)."""
        pid, _, admin_token, user_token, *_ = collab_project
        child_resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Parent with Refs"},
            cookies={"lore_session": admin_token},
        )
        child_id = child_resp.json()["document_id"]
        ref_ids = []
        for i in range(2):
            resp = await client.post(
                "/api/references",
                json={"project_id": pid, "document_id": child_id, "title": f"CascadeRef{i}", "media_type": "markdown"},
                cookies={"lore_session": admin_token},
            )
            ref_ids.append(resp.json()["reference_id"])
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())
            resp = await client.delete(
                f"/api/documents/{child_id}?delete_children=false",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            ref_deleted_ids = set()
            doc_deleted_ids = set()
            for _ in range(1 + len(ref_ids)):
                msg = json.loads(ws.receive_text())
                if msg["type"] == "reference_deleted":
                    ref_deleted_ids.add(msg["reference_id"])
                elif msg["type"] == "document_deleted":
                    doc_deleted_ids.add(msg["document_id"])
            assert doc_deleted_ids == {child_id}
            assert ref_deleted_ids == set(ref_ids)


    @pytest.mark.asyncio
    async def test_transcription_status_changed_reaches_project_ws(self, sync_app, client, collab_project):
        """Transcription complete event → project WS receives reference_status_changed."""
        import asyncio
        pid, *_ = collab_project
        from event_bus import emit
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": collab_project[2]}
        ) as ws:
            json.loads(ws.receive_text())  # init
            await emit("transcription_complete", reference_id="fake-ref-123", project_id=pid)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_status_changed"
            assert msg["reference_id"] == "fake-ref-123"
            assert msg["status"] == "ready"

    @pytest.mark.asyncio
    async def test_transcription_error_reaches_project_ws(self, sync_app, client, collab_project):
        """Transcription error event → project WS receives reference_status_changed with error."""
        import asyncio
        pid, *_ = collab_project
        from event_bus import emit
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": collab_project[2]}
        ) as ws:
            json.loads(ws.receive_text())  # init
            await emit("transcription_error", reference_id="fake-ref-456", project_id=pid)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_status_changed"
            assert msg["reference_id"] == "fake-ref-456"
            assert msg["status"] == "error"


    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, sync_app, collab_project):
        """WS without token is rejected before accept (close code 4001)."""
        from starlette.websockets import WebSocketDisconnect
        pid, *_ = collab_project
        with pytest.raises(WebSocketDisconnect):
            with sync_app.websocket_connect(f"/ws/project/{pid}"):
                pass
