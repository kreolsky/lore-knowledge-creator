"""Integration tests for document routes."""

from unittest.mock import patch

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
async def test_reference_links_returns_mentioned_entities(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = (await client.post("/api/documents", json={"project_id": pid, "title": "D"}, cookies=cookies)).json()["document_id"]
    other_ref = (await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": idx_id, "title": "O", "media_type": "markdown",
              "is_reference": True, "content": "x"},
        cookies=cookies)).json()["document_id"]
    src_ref = (await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": idx_id, "title": "S", "media_type": "markdown",
              "is_reference": True,
              "content": f"text [d]({doc}) ![p](ref:{other_ref})"},
        cookies=cookies)).json()["document_id"]
    resp = await client.get(f"/api/documents/{src_ref}/links", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert doc in data["document_ids"]
    assert other_ref in data["reference_ids"]


@pytest.mark.asyncio
async def test_document_links_prefers_live_session_content_over_db(client, admin_user, project_with_doc):
    """Active collab session with clients must override the stale DB content row.

    Repro Scenario 1 (over-add): doc body was just deleted in-memory; the ~1s
    flush has not yet persisted the empty content. GET /links must resolve the
    first-circle from the LIVE (now empty) session, not the DB row that still
    mentions the reference.
    """
    from types import SimpleNamespace

    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    src = (await client.post("/api/documents", json={"project_id": pid, "title": "Src"}, cookies=cookies)).json()["document_id"]
    ref_id = (await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": idx_id, "title": "R", "media_type": "markdown",
              "is_reference": True, "content": "x"},
        cookies=cookies)).json()["document_id"]
    # DB row mentions the ref.
    await client.patch(f"/api/documents/{src}", json={"content": f"see ![](ref:{ref_id})"}, cookies=cookies)

    # Live session has a connected client but EMPTY content (deletion not yet flushed).
    live = SimpleNamespace(content="", clients={"someone"})

    def fake_active(entity_type, entity_id):
        return live if entity_id == src else None

    with patch("collab.registry.get_active_session", side_effect=fake_active):
        resp = await client.get(f"/api/documents/{src}/links", cookies=cookies)
    assert resp.status_code == 200
    assert ref_id not in resp.json()["reference_ids"], "live empty session must clear the cascade"


@pytest.mark.asyncio
async def test_reference_links_prefers_live_session_content_over_db(client, admin_user, project_with_doc):
    """Mirror of the document test for GET /api/references/{id}/links."""
    from types import SimpleNamespace

    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = (await client.post("/api/documents", json={"project_id": pid, "title": "D"}, cookies=cookies)).json()["document_id"]
    src_ref = (await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": idx_id, "title": "S", "media_type": "markdown",
              "is_reference": True,
              "content": f"text [d]({doc})"},
        cookies=cookies)).json()["document_id"]

    # Live session emptied the reference body before flush.
    live = SimpleNamespace(content="", clients={"someone"})

    def fake_active(entity_type, entity_id):
        return live if entity_id == src_ref else None

    with patch("collab.registry.get_active_session", side_effect=fake_active):
        resp = await client.get(f"/api/references/{src_ref}/links", cookies=cookies)
    assert resp.status_code == 200
    assert doc not in resp.json()["document_ids"], "live empty session must clear the cascade"


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
