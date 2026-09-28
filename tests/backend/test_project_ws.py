"""Tests for project-level WebSocket — tree change notifications.

Verifies that REST mutations emit project-scoped events that reach
connected project WS clients.
"""

import io
import json

import pytest
from starlette.testclient import TestClient

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _connect_project_ws(sync_app: TestClient, pid: str, token: str):
    """Connect to project WS and drain the init message."""
    ws = sync_app.websocket_connect(f"/ws/project/{pid}", cookies={"lore_session": token})
    ctx = ws.__enter__()
    init = json.loads(ctx.receive_text())
    assert init["type"] == "init"
    return ws, ctx


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
    async def test_send_to_user_reaches_only_that_user(self):
        from routes.project_ws import ProjectSession

        session = ProjectSession("p1")
        owner_a, owner_b, member = (
            _FakeProjectClient(), _FakeProjectClient(), _FakeProjectClient(),
        )
        session.add(owner_a, "u1")
        session.add(owner_b, "u1")  # the owner's second tab
        session.add(member, "u2")

        await session.send_to_user("u1", {"type": "chat_frame"})

        assert len(owner_a.sent) == 1 and len(owner_b.sent) == 1
        assert json.loads(owner_a.sent[0]) == {"type": "chat_frame"}
        assert member.sent == []  # the leak actor: outside the delivery set

        # broadcast is unchanged: every client, every identity.
        await session.broadcast({"type": "x"})
        assert len(member.sent) == 1

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
    async def test_document_created_reaches_project_ws(self, sync_app, client, collab_project):
        """POST /api/documents → project WS client receives document_created."""
        pid, _, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/documents",
                json={"project_id": pid, "title": "New Doc"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            doc_id = resp.json()["document_id"]
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "document_created"
            assert msg["document_id"] == doc_id
            assert msg["title"] == "New Doc"

    @pytest.mark.asyncio
    async def test_document_renamed_reaches_project_ws(self, sync_app, client, collab_project):
        """PATCH title → project WS client receives document_renamed."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.patch(
                f"/api/documents/{doc_id}",
                json={"title": "Renamed Doc"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "document_renamed"
            assert msg["document_id"] == doc_id
            assert msg["title"] == "Renamed Doc"

    @pytest.mark.asyncio
    async def test_same_title_patch_emits_no_project_ws_frame(
        self, sync_app, client, collab_project,
    ):
        """Delta (rename-core-one-path): a same-title PATCH emits nothing — no
        document_renamed / content_flushed frame may precede the next real one.
        Sentinel technique: a REAL rename right after the no-op is the frame the
        client must see FIRST (WS delivery is ordered)."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            # No-op: same title as the fixture doc ("Shared Doc").
            resp = await client.patch(
                f"/api/documents/{doc_id}",
                json={"title": "Shared Doc"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            # Real rename — the sentinel.
            resp = await client.patch(
                f"/api/documents/{doc_id}",
                json={"title": "Sentinel Doc"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "document_renamed"
            assert msg["title"] == "Sentinel Doc"
            # The flush of the REAL rename follows; nothing was emitted for the
            # no-op in front of it.
            msg2 = json.loads(ws.receive_text())
            assert msg2["type"] == "content_flushed"
            assert msg2["entity_id"] == doc_id

    @pytest.mark.asyncio
    async def test_document_moved_reaches_project_ws(self, sync_app, client, collab_project):
        """PATCH parent_id → project WS client receives document_moved."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "Parent"},
            cookies={"lore_session": admin_token},
        )
        parent_id = resp.json()["document_id"]
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.patch(
                f"/api/documents/{doc_id}",
                json={"parent_id": parent_id},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "document_moved"
            assert msg["document_id"] == doc_id
            assert msg["parent_id"] == parent_id

    @pytest.mark.asyncio
    async def test_document_deleted_reaches_project_ws(self, sync_app, client, collab_project):
        """DELETE /api/documents?delete_children=false (lift mode) → project WS
        receives document_deleted. The default subtree mode emits a single
        documents_deleted_batch instead (covered by test_subtree_delete.py)."""
        pid, _, admin_token, user_token, *_ = collab_project
        resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "To Delete"},
            cookies={"lore_session": admin_token},
        )
        del_id = resp.json()["document_id"]
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.delete(
                f"/api/documents/{del_id}?delete_children=false",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "document_deleted"
            assert msg["document_id"] == del_id

    @pytest.mark.asyncio
    async def test_reference_created_reaches_project_ws(self, sync_app, client, collab_project):
        """POST /api/references → project WS client receives reference_created."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/references",
                json={"project_id": pid, "document_id": doc_id, "title": "New Ref", "media_type": "markdown"},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            ref_id = resp.json()["reference_id"]
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_created"
            assert msg["reference_id"] == ref_id

    @pytest.mark.asyncio
    async def test_file_upload_reference_created_reaches_project_ws(self, sync_app, client, collab_project):
        """POST /api/references/upload (image) → project WS client receives reference_created."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/references/upload",
                data={"project_id": pid, "document_id": doc_id, "title": "photo.png"},
                files={"file": ("photo.png", io.BytesIO(PNG_BYTES), "image/png")},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            ref_id = resp.json()["reference_id"]
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_created"
            assert msg["reference_id"] == ref_id

    @pytest.mark.asyncio
    async def test_reference_deleted_reaches_project_ws(self, sync_app, client, collab_project):
        """DELETE /api/references → project WS receives reference_deleted."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        resp = await client.post(
            "/api/references",
            json={"project_id": pid, "document_id": doc_id, "title": "Deletable Ref", "media_type": "markdown"},
            cookies={"lore_session": admin_token},
        )
        ref_id = resp.json()["reference_id"]
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.delete(
                f"/api/references/{ref_id}",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_deleted"
            assert msg["reference_id"] == ref_id

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
    async def test_project_deleted_reaches_project_ws(self, sync_app, client, collab_project):
        """DELETE /api/projects → project WS client receives project_deleted."""
        pid, _, admin_token, user_token, *_ = collab_project
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.delete(
                f"/api/projects/{pid}",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "project_deleted"

    @pytest.mark.asyncio
    async def test_reference_updated_reaches_project_ws(self, sync_app, client, collab_project):
        """DELETE /api/references/{id}/file → project WS receives reference_updated."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        # Upload an image ref
        resp = await client.post(
            "/api/references/upload",
            data={"project_id": pid, "document_id": doc_id, "title": "del.png"},
            files={"file": ("del.png", io.BytesIO(PNG_BYTES), "image/png")},
            cookies={"lore_session": admin_token},
        )
        ref_id = resp.json()["reference_id"]
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.delete(
                f"/api/references/{ref_id}/file",
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "reference_updated"
            assert msg["reference_id"] == ref_id

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
    async def test_content_flushed_reaches_project_ws(self, sync_app, client, collab_project):
        """content_flushed event → project WS client receives a stripped payload.

        _make_subscriber drops every field NOT in the subscription list, so the
        payload is {type, entity_id, entity_type} only — is_reference never arrives
        (the frontend gate relies on the local transcludeMap kind, not a payload flag).
        """
        import asyncio
        pid, doc_id, *_ = collab_project
        from event_bus import emit
        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": collab_project[2]}
        ) as ws:
            json.loads(ws.receive_text())  # init
            await emit(
                "content_flushed",
                entity_type="doc", entity_id=doc_id, project_id=pid, is_reference=True,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "content_flushed"
            assert msg["entity_id"] == doc_id
            assert msg["entity_type"] == "doc"
            # INVARIANT: is_reference + project_id are stripped (not in the field list).
            assert "is_reference" not in msg
            assert "project_id" not in msg

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, sync_app, collab_project):
        """WS without token is rejected before accept (close code 4001)."""
        from starlette.websockets import WebSocketDisconnect
        pid, *_ = collab_project
        with pytest.raises(WebSocketDisconnect):
            with sync_app.websocket_connect(f"/ws/project/{pid}"):
                pass
