"""Integration tests for reference routes."""

import json

import pytest
from helpers import join_collab_ws, project_collab_url


@pytest.mark.asyncio
async def test_get_reference_by_id(client, admin_user, project_with_doc):
    """GET /api/references/{id} returns full reference data."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={
            "project_id": pid,
            "title": "Fetchable Ref",
            "media_type": "markdown",
            "content": "# Hello\nWorld",
        },
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    resp = await client.get(
        f"/api/references/{ref_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["reference_id"] == ref_id
    assert data["title"] == "Fetchable Ref"
    assert data["media_type"] == "markdown"
    assert data["content"] == "# Hello\nWorld"
    assert "headings" in data


@pytest.mark.asyncio
async def test_get_reference_not_found(client, admin_user):
    """GET /api/references/{id} for nonexistent ID returns 404."""
    _, token = admin_user
    resp = await client.get(
        "/api/references/nonexistent-id",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_reference(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={
            "project_id": pid,
            "title": "My Ref",
            "media_type": "markdown",
            "content": "# Reference\nSome content",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "My Ref"
    assert data["media_type"] == "markdown"


@pytest.mark.asyncio
async def test_list_references_by_project(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    await client.post(
        "/api/references",
        json={"project_id": pid, "title": "Ref1", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/references?project_id={pid}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    refs = resp.json()
    assert any(r["title"] == "Ref1" for r in refs)


@pytest.mark.asyncio
async def test_list_references_by_document(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Attach ref to index doc
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "DocRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    resp = await client.get(
        f"/api/references?document_id={idx_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    refs = resp.json()
    assert any(r["title"] == "DocRef" for r in refs)


@pytest.mark.asyncio
async def test_patch_reference(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "PatchRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"title": "Updated Ref", "content": "New content"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_reference_content_unchanged_keeps_updated_at(
    client, admin_user, project_with_doc,
):
    """PATCH with content matching DB (or differing only by trailing whitespace) must NOT bump updated_at.

    Why: switching references races WS leave with REST checkpoint fallback; the fallback PATCH
    re-floats the row in the right panel on a pure open/close cycle.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "NoBump", "media_type": "markdown", "content": "Body"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    original = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    original_updated_at = original["updated_at"]

    # Identical content
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": "Body"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    after_identical = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    assert after_identical["updated_at"] == original_updated_at

    # Trailing-newline-only diff
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": "Body\n"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    after_newline = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    assert after_newline["updated_at"] == original_updated_at

    # Real change still bumps
    resp = await client.patch(
        f"/api/references/{ref_id}",
        json={"content": "Body changed"},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    after_change = (await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})).json()
    assert after_change["updated_at"] != original_updated_at


@pytest.mark.asyncio
async def test_delete_reference(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "ToDelete", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    resp = await client.delete(f"/api/references/{ref_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ─── Collab integration tests ────────────────────────────────────────────────


# Fixture sync_app provided by conftest.py


@pytest.mark.asyncio
async def test_patch_reference_ignored_while_clients_connected(sync_app, client, admin_user, project_with_doc):
    """PATCH /references/{id} content is IGNORED while a client is connected.

    Regression: routing the REST autosave content into the live Y.Doc via
    delete-all+insert created all-new CRDT items that diverged from connected
    clients → the text doubled on every re-sync. With clients connected, the
    CRDT (collab WS) is the sole authority; the REST content echo must be a
    no-op. See lessons/2026-05-30.
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "title": "CollabRef", "media_type": "markdown", "content": "Original"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": token}
    ) as ws:
        join_collab_ws(ws, ref_id)
        init = json.loads(ws.receive_text())
        assert init["type"] == "init"
        assert init["entity_id"] == ref_id
        resp = await client.patch(
            f"/api/references/{ref_id}",
            json={"content": "REST patched"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200

        # The live Y.Doc is untouched — the REST content was ignored, not merged.
        from collab.registry import _session_key, _sessions
        session = _sessions[_session_key("doc", ref_id)]
        assert session.content == "Original"


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
async def test_list_references_sorted_own_before_parent(client, admin_user, project_with_doc):
    """References attached to the current document appear before parent's."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create child document under index doc
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Child", "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    child_id = resp.json()["document_id"]
    # Ref on parent (index doc)
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "ParentRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    parent_ref_id = resp.json()["reference_id"]
    # Ref on child
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": child_id, "title": "ChildRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    child_ref_id = resp.json()["reference_id"]
    # List refs for child — own refs should come first
    resp = await client.get(
        f"/api/references?document_id={child_id}",
        cookies={"lore_session": token},
    )
    refs = resp.json()
    ids = [r["reference_id"] for r in refs]
    assert ids.index(child_ref_id) < ids.index(parent_ref_id)


@pytest.mark.asyncio
async def test_list_references_by_nonexistent_document_returns_404(client, admin_user):
    """GET /api/references?document_id=<nonexistent> returns 404, not empty list."""
    _, token = admin_user
    resp = await client.get(
        "/api/references?document_id=nonexistent-id",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_references_sorted_by_ancestry_depth(client, admin_user, project_with_doc):
    """Grandparent refs appear after parent refs (deeper ancestry = lower)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # grandparent = idx_id, parent = mid, child = leaf
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Mid", "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    mid_id = resp.json()["document_id"]
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Leaf", "parent_id": mid_id},
        cookies={"lore_session": token},
    )
    leaf_id = resp.json()["document_id"]
    # Ref on grandparent
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "GrandparentRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    # Ref on parent
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": mid_id, "title": "ParentRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    # Ref on leaf (own)
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": leaf_id, "title": "OwnRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    # List refs for leaf
    resp = await client.get(
        f"/api/references?document_id={leaf_id}",
        cookies={"lore_session": token},
    )
    titles = [r["title"] for r in resp.json()]
    assert titles.index("OwnRef") < titles.index("ParentRef") < titles.index("GrandparentRef")


@pytest.mark.asyncio
async def test_list_references_newest_first_within_same_document(client, admin_user, project_with_doc):
    """Within the same document, newer refs appear before older ones."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create two refs on the same document sequentially
    resp1 = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "Older", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    older_id = resp1.json()["reference_id"]
    resp2 = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "Newer", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    newer_id = resp2.json()["reference_id"]
    # List refs for that document
    resp = await client.get(
        f"/api/references?document_id={idx_id}",
        cookies={"lore_session": token},
    )
    ids = [r["reference_id"] for r in resp.json()]
    assert ids.index(newer_id) < ids.index(older_id)


@pytest.mark.asyncio
async def test_list_references_sorting_combined(client, admin_user, project_with_doc):
    """Older own ref still appears before newer parent ref (depth > time)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Child", "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    child_id = resp.json()["document_id"]
    # Old ref on child (own)
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": child_id, "title": "OldOwn", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    old_own_id = resp.json()["reference_id"]
    # New ref on parent (created after own ref)
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "NewParent", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    new_parent_id = resp.json()["reference_id"]
    # List refs for child — own (older) should still be before parent (newer)
    resp = await client.get(
        f"/api/references?document_id={child_id}",
        cookies={"lore_session": token},
    )
    ids = [r["reference_id"] for r in resp.json()]
    assert ids.index(old_own_id) < ids.index(new_parent_id)


# ─── List is metadata-only (content lazy-fetched) ────────────────────────────


@pytest.mark.asyncio
async def test_list_references_omits_content_carries_has_content(
    client, admin_user, project_with_doc,
):
    """LIST responses are metadata-only: no `content`, no `headings`, but a
    `has_content` boolean that distinguishes an empty ref from a non-empty one.

    Why: the list payload is fetched 2-3x per doc switch at ~360ms prod RTT;
    carrying every ref's full markdown body made each switch multi-MB. Content
    is lazy-fetched per reference via GET /references/{id}.
    """
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "EmptyRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "FullRef", "media_type": "markdown", "content": "# Hi\nbody"},
        cookies={"lore_session": token},
    )
    resp = await client.get(f"/api/references?project_id={pid}", cookies={"lore_session": token})
    assert resp.status_code == 200
    refs = {r["title"]: r for r in resp.json()}
    assert "content" not in refs["EmptyRef"]
    assert "content" not in refs["FullRef"]
    assert "headings" not in refs["FullRef"]
    assert refs["EmptyRef"]["has_content"] is False
    assert refs["FullRef"]["has_content"] is True


@pytest.mark.asyncio
async def test_list_references_has_content_false_for_none_content(
    client, admin_user, project_with_doc, test_db,
):
    """A ref whose DB content is NONE (not "") still reports has_content=False.

    Guards the NONE-safe projection: `string::len(content ?? '') > 0`. A raw
    `string::len(content)` would error on NONE; a truthy check would too.
    """
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "NullRef", "media_type": "markdown"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    # Force the DB content to NONE (the API never produces this, but legacy/imported
    # rows can).
    await test_db.query(
        "UPDATE type::record('documents', $id) SET content = NONE",
        {"id": ref_id},
    )
    resp = await client.get(f"/api/references?project_id={pid}", cookies={"lore_session": token})
    refs = {r["title"]: r for r in resp.json()}
    assert refs["NullRef"]["has_content"] is False


@pytest.mark.asyncio
async def test_get_single_reference_still_carries_content_and_headings(
    client, admin_user, project_with_doc,
):
    """GET /references/{id} (the lazy-fetch endpoint) returns full content + headings."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "FetchMe", "media_type": "markdown", "content": "# Title\nbody"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["reference_id"]
    resp = await client.get(f"/api/references/{ref_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "# Title\nbody"
    assert "headings" in data
    assert len(data["headings"]) >= 1


# ─── Server-side index_doc resolve (regression: broken parent chain) ──────────


@pytest.mark.asyncio
async def test_list_document_scope_includes_index_refs_on_broken_chain(
    client, admin_user, project_with_doc,
):
    """A doc whose parent_id chain does NOT reach the index doc still sees index-level refs.

    Regression guard for the server-side index_doc resolve. get_ancestor_ids walks
    `parent_id` only and can miss the index doc on root-sibling / broken chains, so
    the route always appends the project's index_doc_id itself. The frontend dropped
    the index_doc_id request param (dedup); without this server resolve, index-level
    refs would vanish from the panel for such docs.
    """
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # A ref attached to the project index doc.
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": idx_id, "title": "IndexLevelRef", "media_type": "markdown", "content": "x"},
        cookies={"lore_session": token},
    )
    index_ref_id = resp.json()["reference_id"]
    # A root doc that is NOT the index doc — its ancestor walk is [self], which does
    # NOT include idx_id (mimics a broken/orphan chain). index_doc_id is NOT passed.
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "RootSibling"},
        cookies={"lore_session": token},
    )
    sibling_id = resp.json()["document_id"]
    resp = await client.get(
        f"/api/references?document_id={sibling_id}",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ids = [r["reference_id"] for r in resp.json()]
    assert index_ref_id in ids
