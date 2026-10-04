"""Integration tests for reference-documents (Stage 2).

Covers:
- POST /api/documents with is_reference=true (media_type required).
- Parent constraint: parent_id pointing at a reference is rejected (route-layer).
- Tree query (GET /api/projects/{id}) excludes is_reference=true rows.
- Filter is_reference for batch operations.
- POST /api/documents/batch-delete soft-deletes and rejects mixed-project arrays.
"""

import pytest

# ─── Create + parent constraint ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_reference_requires_media_type(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "is_reference": True, "parent_id": idx_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "media_type" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_create_reference_markdown(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid,
            "is_reference": True,
            "media_type": "markdown",
            "parent_id": idx_id,
            "title": "MyRef",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_reference"] is True
    assert data["parent_id"] == idx_id


@pytest.mark.asyncio
async def test_create_document_under_reference_rejected(
    client, admin_user, project_with_doc,
):
    """Reference documents cannot be parents (route-layer guard)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create a reference first.
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": idx_id,
        },
        cookies={"lore_session": token},
    )
    ref_id = ref_resp.json()["document_id"]

    # Try to create a child under the reference.
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Child", "parent_id": ref_id},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400
    assert "Reference documents cannot be parents" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_patch_move_under_reference_rejected(
    client, admin_user, project_with_doc,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": idx_id,
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    doc_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Mover"},
        cookies=cookies,
    )
    doc_id = doc_resp.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{doc_id}",
        json={"parent_id": ref_id},
        cookies=cookies,
    )
    assert resp.status_code == 400


# ─── Tree exclusion ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_project_tree_excludes_references(
    client, admin_user, project_with_doc,
):
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    # Create one regular doc and one reference under idx.
    reg = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Regular"},
        cookies=cookies,
    )
    regular_id = reg.json()["document_id"]
    ref = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": idx_id,
        },
        cookies=cookies,
    )
    ref_id = ref.json()["document_id"]

    proj = await client.get(f"/api/projects/{pid}", cookies=cookies)
    assert proj.status_code == 200
    doc_ids = {d["document_id"] for d in proj.json()["documents"]}
    assert regular_id in doc_ids
    assert ref_id not in doc_ids


# ─── Batch delete ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_parent_cascades_reference_children(
    client, admin_user, project_with_doc,
):
    """Soft-deleting a regular document cascades to its reference-document children."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    parent_resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "Parent"},
        cookies=cookies,
    )
    parent_id = parent_resp.json()["document_id"]
    ref_resp = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": parent_id,
        },
        cookies=cookies,
    )
    ref_id = ref_resp.json()["document_id"]

    del_resp = await client.delete(f"/api/documents/{parent_id}", cookies=cookies)
    assert del_resp.status_code == 200

    # Reference-child should now be soft-deleted.
    get_ref = await client.get(f"/api/documents/{ref_id}", cookies=cookies)
    assert get_ref.status_code == 404


@pytest.mark.asyncio
async def test_batch_delete_documents(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    ids = []
    for i in range(3):
        r = await client.post(
            "/api/documents",
            json={"project_id": pid, "title": f"D{i}"},
            cookies=cookies,
        )
        ids.append(r.json()["document_id"])
    resp = await client.post(
        "/api/documents/batch-delete",
        json={"document_ids": ids},
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 3
    assert body["skipped"] == 0
    # Subsequent GETs should 404.
    for did in ids:
        r = await client.get(f"/api/documents/{did}", cookies=cookies)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_no_auto_backup_for_reference(client, admin_user, project_with_doc):
    """maybe_backup_on_content_loss() must return None for is_reference documents."""
    from auto_backup import maybe_backup_on_content_loss
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    # Create a reference with long content so the BACKUP_MIN_CONTENT gate alone
    # would not skip the backup.
    long_text = "abc " * 200
    ref = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": idx_id,
            "content": long_text,
        },
        cookies={"lore_session": token},
    )
    ref_id = ref.json()["document_id"]

    # Even a destructive replace (most of the content gone) must not trigger a backup.
    result = await maybe_backup_on_content_loss(ref_id, "x")
    assert result is None


@pytest.mark.asyncio
async def test_rest_patch_no_backup_for_reference(client, admin_user, project_with_doc):
    """PATCH on a reference document with a destructive content delta must not
    create a checkpoint (references never auto-backup, even via REST)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    long_text = "abc " * 200
    ref = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True,
            "media_type": "markdown", "parent_id": idx_id,
            "content": long_text,
        },
        cookies=cookies,
    )
    ref_id = ref.json()["document_id"]

    resp = await client.patch(
        f"/api/documents/{ref_id}",
        json={"content": "x"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    assert resp.json().get("auto_backup") in (None, {})

    cps = await client.get(f"/api/checkpoints?document_id={ref_id}", cookies=cookies)
    assert cps.json() == []


@pytest.mark.asyncio
async def test_batch_delete_skips_unknown(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": "ToDelete"},
        cookies=cookies,
    )
    did = r.json()["document_id"]
    resp = await client.post(
        "/api/documents/batch-delete",
        json={"document_ids": [did, "00000000-0000-0000-0000-000000000000"]},
        cookies=cookies,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 1
    assert body["skipped"] == 1


# ─── Cascade delete: record-targeted writes ──────────────────────────────────


async def _mk_doc(client, pid, token, *, title, parent_id=None):
    r = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "parent_id": parent_id},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _mk_ref(client, pid, token, parent_id, title):
    r = await client.post(
        "/api/documents",
        json={
            "project_id": pid, "is_reference": True, "media_type": "markdown",
            "parent_id": parent_id, "title": title, "content": "ref body",
        },
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _deleted_at(test_db, table, rid):
    rows = await test_db.query(
        f"SELECT deleted_at FROM type::record('{table}', $id)", {"id": rid},
    )
    return rows[0].get("deleted_at") if rows and rows[0] else None


@pytest.mark.asyncio
async def test_document_delete_soft_deletes_hosted_refs_only(
    client, test_db, admin_user, project_with_doc,
):
    """DELETE of a hosting document soft-deletes exactly its hosted references,
    leaves another project's document untouched, and creates no new documents row
    (the cascade's record-targeted UPDATE is a strict no-op for absent ids)."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    host = await _mk_doc(client, pid, token, title="Host", parent_id=idx_id)
    ref_ids = [await _mk_ref(client, pid, token, host, f"Ref{i}") for i in range(5)]

    other_proj = await client.post(
        "/api/projects", json={"name": "Other Project"}, cookies=cookies,
    )
    assert other_proj.status_code == 200, other_proj.text
    other_doc = await _mk_doc(client, other_proj.json()["project_id"], token, title="OtherDoc")

    count_before = await test_db.query("SELECT VALUE count() FROM documents GROUP ALL")

    resp = await client.delete(f"/api/documents/{host}", cookies=cookies)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 6

    for rid in ref_ids:
        assert await _deleted_at(test_db, "documents", rid) is not None, \
            f"{rid} must be soft-deleted"
    assert await _deleted_at(test_db, "documents", other_doc) is None, \
        "another project's document must be untouched"

    count_after = await test_db.query("SELECT VALUE count() FROM documents GROUP ALL")
    assert count_after == count_before, "cascade delete must create no documents row"


@pytest.mark.asyncio
async def test_cascade_soft_deletes_chat_sessions_by_id(
    client, test_db, admin_user, project_with_doc,
):
    """Deleting a document soft-deletes its chat sessions AND their messages,
    targeted by record id (no table scan)."""
    from uuid import uuid4

    from db import create_record

    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    doc = await _mk_doc(client, pid, token, title="ChatHost", parent_id=idx_id)
    session_ids = [str(uuid4()) for _ in range(2)]
    for sid in session_ids:
        await create_record("chat_sessions", sid, {
            "project_id": pid, "document_id": doc, "user_id": "cascade-user",
        })
        await create_record("messages", str(uuid4()), {
            "chat_id": sid, "role": "user", "content": "hi",
        })

    resp = await client.delete(f"/api/documents/{doc}", cookies=cookies)
    assert resp.status_code == 200, resp.text

    for sid in session_ids:
        assert await _deleted_at(test_db, "chat_sessions", sid) is not None, \
            f"chat session {sid} must be soft-deleted"
    live_msgs = await test_db.query(
        "SELECT VALUE count() FROM messages "
        "WHERE chat_id IN $cids AND deleted_at IS NONE GROUP ALL",
        {"cids": session_ids},
    )
    assert not live_msgs or live_msgs[0] == 0, "chat messages must be soft-deleted"


@pytest.mark.asyncio
async def test_cascade_deletes_inbound_and_outbound_mentions(
    client, test_db, admin_user, project_with_doc,
):
    """Deleting a document removes BOTH its inbound and outbound doc_mentions edges
    and leaves unrelated edges alone."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}

    a = await _mk_doc(client, pid, token, title="A", parent_id=idx_id)
    b = await _mk_doc(client, pid, token, title="B", parent_id=idx_id)
    c = await _mk_doc(client, pid, token, title="C", parent_id=idx_id)
    # Outbound A→B, inbound C→A, unrelated B→C. ids are server UUIDs — ⟨⟩ escape
    # the dashes for the record-literal parse.
    await test_db.query(f"RELATE documents:⟨{a}⟩->doc_mentions->documents:⟨{b}⟩")
    await test_db.query(f"RELATE documents:⟨{c}⟩->doc_mentions->documents:⟨{a}⟩")
    await test_db.query(f"RELATE documents:⟨{b}⟩->doc_mentions->documents:⟨{c}⟩")

    async def edge_count(src, dst):
        rows = await test_db.query(
            "SELECT VALUE count() FROM doc_mentions "
            "WHERE in = type::record('documents', $src) "
            "AND out = type::record('documents', $dst) GROUP ALL",
            {"src": src, "dst": dst},
        )
        return rows[0] if rows else 0

    resp = await client.delete(f"/api/documents/{a}", cookies=cookies)
    assert resp.status_code == 200, resp.text

    assert await edge_count(a, b) == 0, "outbound mention of the deleted doc must be gone"
    assert await edge_count(c, a) == 0, "inbound mention of the deleted doc must be gone"
    assert await edge_count(b, c) == 1, "unrelated mention edge must survive"


@pytest.mark.asyncio
async def test_record_targeted_update_on_missing_id_is_a_noop(
    client, test_db, admin_user, project_with_doc,
):
    """PIN: the delete path's record-targeted tombstone form relies on SurrealDB
    treating an absent record id as a silent no-op (cascade.py WHY block). A
    server/SDK bump that turns the absent id into an error would 500 every
    delete — this test makes that drift fail loudly here instead."""
    pid, idx_id, _ = project_with_doc
    _, token = admin_user
    live_id = await _mk_doc(client, pid, token, title="NoopPin", parent_id=idx_id)
    # uuid4-shaped but never created.
    missing = "00000000-0000-4000-8000-000000000000"
    count_before = await test_db.query("SELECT VALUE count() FROM documents GROUP ALL")

    # The exact production form (cascade.py, documents/delete.py).
    await test_db.query(
        "UPDATE $rids.map(|$x| type::record('documents', $x)) "
        "SET deleted_at = time::now() WHERE deleted_at IS NONE",
        {"rids": [live_id, missing]},
    )

    count_after = await test_db.query("SELECT VALUE count() FROM documents GROUP ALL")
    assert count_after == count_before, "an absent id must not materialize a row"
    assert await _deleted_at(test_db, "documents", live_id) is not None
    assert await _deleted_at(test_db, "documents", missing) is None

    # Same contract for the chat_sessions variant ($cids form).
    await test_db.query(
        "UPDATE $rids.map(|$x| type::record('chat_sessions', $x)) "
        "SET deleted_at = time::now() WHERE deleted_at IS NONE",
        {"rids": [missing]},
    )
    assert await _deleted_at(test_db, "chat_sessions", missing) is None
