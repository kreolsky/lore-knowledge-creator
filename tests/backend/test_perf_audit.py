"""Tests for backend audit batch 2 — performance fixes P-1, P-2, P-4."""

import uuid

import pytest

# ─── P-1: Batch cascade delete ─────────────────────────────────────────────────
# Note: Notes cascade-delete tests removed — notes are now chat_sessions,
# cascade is tested via chat session deletion.


@pytest.mark.asyncio
async def test_chat_messages_pagination(client, admin_user, project_with_doc):
    """GET /api/chat/sessions/{id}/messages supports limit and offset."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/chat/sessions", json={"project_id": pid, "document_id": doc_id}, cookies=cookies)
    sid = resp.json()["session_id"]

    # Create 5 messages
    for i in range(5):
        await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"role": "user", "content": f"msg{i}"},
            cookies=cookies,
        )

    # Default — all messages returned
    resp = await client.get(f"/api/chat/sessions/{sid}/messages", cookies=cookies)
    assert len(resp.json()) == 5

    # Limit to 2
    resp = await client.get(f"/api/chat/sessions/{sid}/messages?limit=2", cookies=cookies)
    assert len(resp.json()) == 2

    # Offset 3, limit 10 — should get 2 remaining
    resp = await client.get(f"/api/chat/sessions/{sid}/messages?offset=3&limit=10", cookies=cookies)
    assert len(resp.json()) == 2


# Note: notes/ref-notes pagination tests removed — notes are now chat_sessions.


@pytest.mark.asyncio
async def test_checkpoints_pagination(client, admin_user, project_with_doc):
    """GET /api/checkpoints supports limit and offset."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/documents", json={"project_id": pid, "title": "PagCp"}, cookies=cookies)
    doc_id = resp.json()["document_id"]

    for i in range(5):
        await client.post("/api/checkpoints", json={"document_id": doc_id, "label": f"cp{i}"}, cookies=cookies)

    resp = await client.get(f"/api/checkpoints?document_id={doc_id}", cookies=cookies)
    assert len(resp.json()) == 5

    resp = await client.get(f"/api/checkpoints?document_id={doc_id}&limit=3", cookies=cookies)
    assert len(resp.json()) == 3

    resp = await client.get(f"/api/checkpoints?document_id={doc_id}&limit=10&offset=4", cookies=cookies)
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_references_pagination(client, admin_user, project_with_doc):
    """GET /api/references supports limit and offset."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    for i in range(5):
        await client.post(
            "/api/references",
            json={"project_id": pid, "title": f"Ref{i}", "media_type": "markdown"},
            cookies=cookies,
        )

    resp = await client.get(f"/api/references?project_id={pid}", cookies=cookies)
    assert len(resp.json()) == 5

    resp = await client.get(f"/api/references?project_id={pid}&limit=2", cookies=cookies)
    assert len(resp.json()) == 2

    resp = await client.get(f"/api/references?project_id={pid}&limit=10&offset=4", cookies=cookies)
    assert len(resp.json()) == 1


# Note: ref_notes_pagination removed — notes are now chat_sessions.


# ─── P-4: Reduced get_document queries ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_document_updates_last_accessed(client, admin_user, regular_user, project_with_doc):
    """get_document updates last_accessed_doc_id visible via project endpoint."""
    from db import create_record

    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    member_id, member_token = regular_user

    # Add regular_user as a project member (owner skips last_accessed tracking)
    pm_id = str(uuid.uuid4())
    await create_record("project_members", pm_id, {
        "project_id": pid,
        "user_id": member_id,
        "access_level": "full",
    })

    cookies = {"lore_session": member_token}

    # Create a second document
    resp = await client.post("/api/documents", json={"project_id": pid, "title": "Second"}, cookies=cookies)
    doc2_id = resp.json()["document_id"]

    # Access first doc, then second (track=1 = intentional open updates last_accessed)
    await client.get(f"/api/documents/{idx_id}?track=1", cookies=cookies)
    await client.get(f"/api/documents/{doc2_id}?track=1", cookies=cookies)

    # Project endpoint returns last_accessed_doc_id
    resp = await client.get(f"/api/projects/{pid}", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["project"].get("last_accessed_doc_id") == doc2_id


@pytest.mark.asyncio
async def test_get_document_functional_correctness(client, admin_user, project_with_doc):
    """get_document returns correct data after query optimization."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.get(f"/api/documents/{idx_id}", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_id"] == idx_id
    assert data["title"] == "project_context.md"
    assert "headings" in data
