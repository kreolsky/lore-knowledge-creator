"""4.2 — Batch event for cascade delete.

Batch-delete emits a single documents_deleted_batch event instead of per-document
document_deleted / reference_deleted / entity_deleted. The project WS client
receives one batch broadcast, not N individual ones.
"""

import json

import pytest


class TestBatchDeleteEvent:
    @pytest.mark.asyncio
    async def test_batch_delete_emits_batch_event(self, sync_app, client, collab_project):
        """POST /api/documents/batch-delete → project WS receives documents_deleted_batch."""
        pid, _, admin_token, user_token, *_ = collab_project
        ids = []
        for i in range(3):
            resp = await client.post(
                "/api/documents",
                json={"project_id": pid, "title": f"BatchDel{i}"},
                cookies={"lore_session": admin_token},
            )
            ids.append(resp.json()["document_id"])

        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/documents/batch-delete",
                json={"document_ids": ids},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200

            # Exactly ONE message: the batch event (no individual document_deleted).
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "documents_deleted_batch"
            assert set(msg["document_ids"]) == set(ids)
            assert msg["reference_ids"] == []

    @pytest.mark.asyncio
    async def test_batch_delete_with_references(self, sync_app, client, collab_project):
        """Batch-delete including references → batch event carries reference_ids."""
        pid, doc_id, admin_token, user_token, *_ = collab_project
        doc_resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "RegDoc"},
            cookies={"lore_session": admin_token},
        )
        reg_id = doc_resp.json()["document_id"]
        ref_resp = await client.post(
            "/api/documents",
            json={
                "project_id": pid, "parent_id": doc_id, "title": "RefDoc",
                "media_type": "markdown", "is_reference": True,
            },
            cookies={"lore_session": admin_token},
        )
        ref_id = ref_resp.json()["document_id"]

        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/documents/batch-delete",
                json={"document_ids": [reg_id, ref_id]},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "documents_deleted_batch"
            assert set(msg["document_ids"]) == {reg_id, ref_id}
            assert set(msg["reference_ids"]) == {ref_id}

    @pytest.mark.asyncio
    async def test_batch_delete_cascades_descendant_references(
        self, sync_app, client, collab_project
    ):
        """Batch-deleting only a parent must broadcast its cascade-deleted child reference.

        Since subtree-delete (delete_children=true default), the descendant reference
        is part of the EXPANDED id set: it rides document_ids (it IS a tombstoned
        subtree row) and reference_ids (so the frontend drops its ghost reference
        entry / stale chat session). Lift mode keeps the legacy split — the ref is
        cascade-deleted, reported in reference_ids only.
        """
        pid, _, admin_token, user_token, *_ = collab_project
        parent_resp = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": "ParentDoc"},
            cookies={"lore_session": admin_token},
        )
        parent_id = parent_resp.json()["document_id"]
        child_ref_resp = await client.post(
            "/api/documents",
            json={
                "project_id": pid, "parent_id": parent_id, "title": "ChildRef",
                "media_type": "markdown", "is_reference": True,
            },
            cookies={"lore_session": admin_token},
        )
        child_ref_id = child_ref_resp.json()["document_id"]

        with sync_app.websocket_connect(
            f"/ws/project/{pid}", cookies={"lore_session": user_token}
        ) as ws:
            json.loads(ws.receive_text())  # init
            resp = await client.post(
                "/api/documents/batch-delete",
                json={"document_ids": [parent_id], "delete_children": False},
                cookies={"lore_session": admin_token},
            )
            assert resp.status_code == 200

            msg = json.loads(ws.receive_text())
            assert msg["type"] == "documents_deleted_batch"
            assert set(msg["document_ids"]) == {parent_id}
            assert child_ref_id in msg["reference_ids"]
            assert child_ref_id not in msg["document_ids"]
