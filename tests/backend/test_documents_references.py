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
