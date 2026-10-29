"""Integration tests for reference routes."""

import json

import pytest
from helpers import join_collab_ws, project_collab_url

# ─── Collab integration tests ────────────────────────────────────────────────


# Fixture sync_app provided by conftest.py


@pytest.mark.asyncio
async def test_delete_reference_notifies_collab_session(sync_app, client, admin_user, project_with_doc):
    """DELETE /references/{id} sends entity_deleted to connected WS clients."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "DoomeRef", "media_type": "markdown", "content": "bye"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": token}
    ) as ws:
        join_collab_ws(ws, ref_id)
        json.loads(ws.receive_text())  # init (join ack)
        # Delete via REST
        resp = await client.delete(
            f"/api/references/{ref_id}",
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        # WS client should receive doc_deleted (entity deleted)
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "doc_deleted"


@pytest.mark.asyncio
async def test_patch_reference_rebuilds_mention_edges(client, admin_user, project_with_doc):
    """PATCH reference content with [text](docId) link creates doc_mentions edges."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create a second doc to be mentioned
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Target"},
        cookies={"lore_session": token},
    )
    target_id = resp.json()["document_id"]
    # Create reference
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "MentionRef", "media_type": "markdown", "content": "empty"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    # Patch content with doc link
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": f"See [Target]({target_id})"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Verify backlink exists on target doc
    resp = await client.get(
        f"/api/documents/{target_id}/backlinks",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    backlinks = resp.json()["backlinks"]
    # Backlink should show the reference as source
    ref_sources = [bl for bl in backlinks if bl.get("reference_id") == ref_id or bl.get("source_type") == "ref"]
    assert len(ref_sources) >= 1
    # (exact shape depends on how backlinks query handles ref-sourced edges)


# ─── Sorting tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_references_by_nonexistent_document_returns_404(client, admin_user):
    """GET /api/references?document_id=<nonexistent> returns 404, not empty list."""
    _, token = admin_user
    resp = await client.get(
        "/api/references?document_id=nonexistent-id",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404
