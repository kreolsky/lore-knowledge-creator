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
