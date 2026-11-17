"""Integration tests for document routes."""


import pytest


@pytest.mark.asyncio
async def test_create_document_auto_title(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Untitled"
    assert data["path"] == "untitled.md"


@pytest.mark.asyncio
async def test_auto_backup_skips_small_edits(client, admin_user, project_with_doc):
    """Small edits in a large document do not trigger auto-backup."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    content = "A" * 2000
    resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "Big Doc", "content": content,
    }, cookies=cookies)
    doc_id = resp.json()["document_id"]

    # Remove 20 chars (1%) — should not trigger
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": content[:1980]}, cookies=cookies,
    )
    assert resp.status_code == 200
    assert resp.json().get("auto_backup") is None
