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
async def test_create_document_with_title(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "My Document"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "My Document"
    assert data["path"] == "my-document.md"


@pytest.mark.asyncio
async def test_get_document(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.get(f"/api/documents/{idx_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "project_context.md"
    assert "headings" in data


@pytest.mark.asyncio
async def test_patch_document_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Create a doc to patch
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Patchable"},
        cookies={"lore_session": token},
    )
    doc_id = resp.json()["document_id"]
    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "# Hello\n\nSome text"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_id"] == doc_id
    assert len(data["headings"]) == 1
    assert data["headings"][0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_auto_backup_mass_deletion(client, admin_user, project_with_doc, enqueue_recorder):
    """Deleting most of a large document creates an inline auto-backup, returned in the
    PATCH response (the no-collab REST path stays inline, not enqueued — see plan #6)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Backup Del"},
        cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "A" * 2000},
        cookies=cookies,
    )

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "A" * 100},
        cookies=cookies,
    )
    assert resp.status_code == 200
    assert "auto_backup_loss_task" not in enqueue_recorder.names()
    assert resp.json().get("auto_backup")


@pytest.mark.asyncio
async def test_auto_backup_mass_replacement(client, admin_user, project_with_doc, enqueue_recorder):
    """Replacing all content with different text (same length) creates an inline
    auto-backup, returned in the PATCH response (REST path stays inline — see plan #6)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Backup Repl"},
        cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "A" * 1000},
        cookies=cookies,
    )

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": "B" * 1000},
        cookies=cookies,
    )
    assert resp.status_code == 200
    assert "auto_backup_loss_task" not in enqueue_recorder.names()
    assert resp.json().get("auto_backup")


@pytest.mark.asyncio
async def test_backlinks(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Create two docs
    resp1 = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Target"},
        cookies={"lore_session": token},
    )
    target_id = resp1.json()["document_id"]
    resp2 = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Source"},
        cookies={"lore_session": token},
    )
    source_id = resp2.json()["document_id"]
    # Source links to Target
    await client.patch(
        f"/api/documents/{source_id}",
        json={"content": f"See [target]({target_id})"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/documents/{target_id}/backlinks",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    backlinks = resp.json()["backlinks"]
    assert any(b["document_id"] == source_id for b in backlinks)


@pytest.mark.asyncio
async def test_document_links_returns_only_mentioned_refs(client, admin_user, project_with_doc):
    """Document with N owned refs but body mentioning only one → endpoint returns only that one."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Owner"},
        cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    ref_ids = []
    for i in range(3):
        r = await client.post(
            "/api/documents",
            json={"project_id": pid, "parent_id": doc_id, "title": f"R{i}",
                  "media_type": "markdown", "is_reference": True, "content": "x"},
            cookies=cookies,
        )
        ref_ids.append(r.json()["document_id"])
    mentioned = ref_ids[1]
    await client.patch(
        f"/api/documents/{doc_id}",
        json={"content": f"see ![pic](ref:{mentioned})"},
        cookies=cookies,
    )
    resp = await client.get(f"/api/documents/{doc_id}/links", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["reference_ids"] == [mentioned]
    assert data["document_ids"] == []


@pytest.mark.asyncio
async def test_document_links_returns_mentioned_docs(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    target = (await client.post("/api/documents", json={"project_id": pid, "title": "T"}, cookies=cookies)).json()["document_id"]
    src = (await client.post("/api/documents", json={"project_id": pid, "title": "S"}, cookies=cookies)).json()["document_id"]
    await client.patch(f"/api/documents/{src}", json={"content": f"see [t]({target})"}, cookies=cookies)
    resp = await client.get(f"/api/documents/{src}/links", cookies=cookies)
    assert resp.status_code == 200
    assert target in resp.json()["document_ids"]


@pytest.mark.asyncio
async def test_document_links_excludes_self(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc = (await client.post("/api/documents", json={"project_id": pid, "title": "Me"}, cookies=cookies)).json()["document_id"]
    await client.patch(f"/api/documents/{doc}", json={"content": f"loop [me]({doc})"}, cookies=cookies)
    resp = await client.get(f"/api/documents/{doc}/links", cookies=cookies)
    assert doc not in resp.json()["document_ids"]


@pytest.mark.asyncio
async def test_document_links_excludes_deleted(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    src = (await client.post("/api/documents", json={"project_id": pid, "title": "Src"}, cookies=cookies)).json()["document_id"]
    ref_id = (await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": idx_id, "title": "R", "media_type": "markdown",
              "is_reference": True, "content": "x"},
        cookies=cookies)).json()["document_id"]
    await client.patch(f"/api/documents/{src}", json={"content": f"![](ref:{ref_id})"}, cookies=cookies)
    await client.delete(f"/api/documents/{ref_id}", cookies=cookies)
    resp = await client.get(f"/api/documents/{src}/links", cookies=cookies)
    assert ref_id not in resp.json()["reference_ids"]


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
async def test_document_links_falls_back_to_db_when_session_has_no_clients(client, admin_user, project_with_doc):
    """A session with no connected clients must NOT override DB content (idle session)."""
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
    await client.patch(f"/api/documents/{src}", json={"content": f"see ![](ref:{ref_id})"}, cookies=cookies)

    # Idle session: present but no clients → DB content is authoritative.
    idle = SimpleNamespace(content="", clients={})

    with patch("collab.registry.get_active_session", return_value=idle):
        resp = await client.get(f"/api/documents/{src}/links", cookies=cookies)
    assert resp.status_code == 200
    assert ref_id in resp.json()["reference_ids"]


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
async def test_delete_document_reparents_children(client, admin_user, project_with_doc):
    """Lift mode (delete_children=false): children reparent to the grandparent.
    The default (subtree) mode is covered by test_subtree_delete.py."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    # Create parent → child hierarchy
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Parent"},
        cookies={"lore_session": token},
    )
    parent_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Child", "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    child_id = resp.json()["document_id"]
    # Delete parent
    resp = await client.delete(
        f"/api/documents/{parent_id}?delete_children=false",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # Child should be reparented to None (root)
    resp = await client.get(f"/api/documents/{child_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json().get("parent_id") is None


@pytest.mark.asyncio
async def test_duplicate_title_gets_unique_path(client, admin_user, project_with_doc):
    """Two documents with the same title in one project get different paths."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp1 = await client.post("/api/documents", json={"project_id": pid, "title": "Chapter One"}, cookies=cookies)
    assert resp1.status_code == 200
    resp2 = await client.post("/api/documents", json={"project_id": pid, "title": "Chapter One"}, cookies=cookies)
    assert resp2.status_code == 200

    path1 = resp1.json().get("path")
    path2 = resp2.json().get("path")
    assert path1 != path2


@pytest.mark.asyncio
async def test_auto_backup_skips_short_docs(client, admin_user, project_with_doc):
    """Documents below BACKUP_MIN_CONTENT never trigger auto-backup."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "Short Doc", "content": "x" * 100,
    }, cookies=cookies)
    doc_id = resp.json()["document_id"]

    # A heavy delete on a short doc should not trigger backup (baseline < 200).
    # Full-empty bodies are pinned separately: the empty-wipe guard 409s them
    # (test_empty_wipe_guard.py) — no longer a valid vehicle here.
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": "x" * 10}, cookies=cookies,
    )
    assert resp.status_code == 200
    assert resp.json().get("auto_backup") is None


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


@pytest.mark.asyncio
async def test_auto_backup_skips_append(client, admin_user, project_with_doc):
    """Appending content (no loss) does not trigger auto-backup."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    content = "A" * 1000
    resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "Append Doc", "content": content,
    }, cookies=cookies)
    doc_id = resp.json()["document_id"]

    # Append 2000 chars — nothing lost
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": content + "B" * 2000}, cookies=cookies,
    )
    assert resp.status_code == 200
    assert resp.json().get("auto_backup") is None
