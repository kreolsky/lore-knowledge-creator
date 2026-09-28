"""Integration tests for Phase 1 blob-backed checkpoint storage."""

import pytest


async def _make_doc_with_checkpoint(client, token, pid, content="Blob test content"):
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "BlobDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": content}, cookies=cookies)
    resp = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    return doc_id, resp.json()["checkpoint_id"]


@pytest.mark.asyncio
async def test_list_does_not_return_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Secret content")

    resp = await client.get(
        f"/api/checkpoints?document_id={doc_id}", cookies=cookies,
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    cp = next(r for r in rows if r["checkpoint_id"] == cp_id)
    assert "content" not in cp or cp.get("content") is None


@pytest.mark.asyncio
async def test_get_single_checkpoint_returns_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Lazy load me")

    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    assert resp.status_code == 200
    data = resp.json()
    assert data["checkpoint_id"] == cp_id
    assert data["content"] == "Lazy load me"


@pytest.mark.asyncio
async def test_get_single_checkpoint_not_found(client, admin_user, project_with_doc):
    _, token = admin_user
    resp = await client.get(
        "/api/checkpoints/nonexistent-id", cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_single_checkpoint_read_access(client, admin_user, project_with_doc, regular_user):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    _, user_token = regular_user
    doc_id, cp_id = await _make_doc_with_checkpoint(client, admin_token, pid, "Admin content")

    from db import create_record
    await create_record("project_members", "test-pm-reader", {
        "project_id": pid,
        "user_id": "test-user-001",
        "access_level": "readonly",
    })

    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies={"lore_session": user_token})
    assert resp.status_code == 200
    assert resp.json()["content"] == "Admin content"


@pytest.mark.asyncio
async def test_create_checkpoint_stores_blob(client, admin_user, project_with_doc, test_db):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "BlobCreate"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Stored in blob"}, cookies=cookies)

    resp = await client.post(
        "/api/checkpoints",
        json={"document_id": doc_id, "label": "v1"},
        cookies=cookies,
    )
    assert resp.status_code == 200
    cp = resp.json()

    content_ref = cp.get("content_ref")
    assert content_ref is not None

    from cp_store import hash_content
    assert content_ref == hash_content("Stored in blob")

    blobs = await test_db.query(
        "SELECT id FROM type::record('cp_blobs', $ref)",
        {"ref": content_ref},
    )
    assert blobs


@pytest.mark.asyncio
async def test_restore_uses_blob_content(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Blob restore me")

    await client.patch(f"/api/documents/{doc_id}", json={"content": "Overwritten"}, cookies=cookies)

    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200

    resp = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert resp.json()["content"] == "Blob restore me"


@pytest.mark.asyncio
async def test_dedup_same_content_reuses_blob(client, admin_user, project_with_doc, test_db):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    resp = await client.post(
        "/api/documents", json={"project_id": pid, "title": "DedupDoc"}, cookies=cookies,
    )
    doc_id = resp.json()["document_id"]
    await client.patch(f"/api/documents/{doc_id}", json={"content": "Same content"}, cookies=cookies)

    resp1 = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v1"}, cookies=cookies,
    )
    resp2 = await client.post(
        "/api/checkpoints", json={"document_id": doc_id, "label": "v2"}, cookies=cookies,
    )
    ref1 = resp1.json()["content_ref"]
    ref2 = resp2.json()["content_ref"]
    assert ref1 == ref2

    blobs = await test_db.query("SELECT count() AS c FROM cp_blobs GROUP ALL")
    assert blobs and blobs[0]["c"] == 1


@pytest.mark.asyncio
async def test_validate_with_blob(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Intact via blob")

    resp = await client.get(f"/api/checkpoints/{cp_id}/validate", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


@pytest.mark.asyncio
async def test_backfill_migrates_soft_deleted_rows(test_db):
    """Soft-deleted legacy rows must also get a content_ref.

    Their content lives in cp_blobs forever; the upcoming null-out of inline
    `content` would otherwise destroy data on soft-deleted (thinned) rows.
    """
    from datetime import datetime, timezone

    from cp_store import hash_content

    from db import create_record
    from jobs.tasks import backfill_cp_blobs_task

    text = "Soft deleted legacy content"
    await create_record("checkpoints", "test-cp-softdel", {
        "document_id": "test-doc-softdel",
        "content": text,
        "content_hash": hash_content(text),
        "label": "auto-backup",
        "deleted_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
    })

    await backfill_cp_blobs_task({})

    rows = await test_db.query(
        "SELECT content_ref FROM type::record('checkpoints', 'test-cp-softdel')",
    )
    assert rows and rows[0]["content_ref"] == hash_content(text)


@pytest.mark.asyncio
async def test_legacy_inline_fallback(client, admin_user, project_with_doc, test_db):
    pid, _, _ = project_with_doc
    _, token = admin_user
    cookies = {"lore_session": token}
    doc_id, cp_id = await _make_doc_with_checkpoint(client, token, pid, "Legacy fallback")

    # Simulate a genuine pre-migration row: inline content but no content_ref.
    # The writer stores content ONLY in the blob (C3-A), so a fresh row has no
    # inline content; the inline-fallback read path exists for legacy rows, which
    # we reconstruct here by nulling content_ref and restoring the inline text.
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) "
        "SET content_ref = NONE, content = $c, content_hash = NONE",
        {"id": cp_id, "c": "Legacy fallback"},
    )

    resp = await client.get(f"/api/checkpoints/{cp_id}", cookies=cookies)
    assert resp.status_code == 200
    assert resp.json()["content"] == "Legacy fallback"

    await client.patch(f"/api/documents/{doc_id}", json={"content": "Different"}, cookies=cookies)
    resp = await client.post(f"/api/checkpoints/{cp_id}/restore", cookies=cookies)
    assert resp.status_code == 200

    resp = await client.get(f"/api/documents/{doc_id}", cookies=cookies)
    assert resp.json()["content"] == "Legacy fallback"
