"""Integration tests for content_flushed event → reembed debounce pipeline."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_content_flushed_emitted_on_patch(client, admin_user, project_with_doc):
    """PATCH document content → content_flushed event fires."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    from event_bus import off, on

    calls = []
    async def handler(**kwargs):
        calls.append(kwargs)

    on("content_flushed", handler)
    try:
        resp = await client.patch(
            f"/api/documents/{doc_id}",
            json={"content": "Updated content for flush"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        assert len(calls) >= 1
        assert calls[0]["entity_type"] == "doc"
        assert calls[0]["entity_id"] == doc_id
    finally:
        off("content_flushed", handler)


@pytest.mark.asyncio
async def test_content_flushed_ref_title(client, admin_user, project_with_doc):
    """PATCH ref title → content_flushed event fires."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user

    resp = await client.post(
        "/api/references",
        json={
            "project_id": pid,
            "document_id": doc_id,
            "title": "Original Title",
            "media_type": "markdown",
            "content": "Some content",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ref_id = resp.json()["reference_id"]

    from event_bus import emit, off, on

    received = []
    async def catcher(**kwargs):
        received.append(kwargs)

    on("content_flushed", catcher)
    try:
        await emit("content_flushed", entity_type="ref", entity_id=ref_id, project_id=pid)
        await asyncio.sleep(0)  # yield to event loop — emit is fire-and-forget (create_task)
        assert len(received) == 1
        assert received[0]["entity_type"] == "ref"
    finally:
        off("content_flushed", catcher)


@pytest.mark.asyncio
async def test_content_flushed_enqueues_embed_task(enqueue_recorder):
    """content_flushed handler enqueues embed_document_task via arq."""
    from embeddings import _on_content_flushed

    class FakeRedis:
        async def set(self, key, value, **kwargs):
            pass

    from unittest.mock import patch
    with patch("jobs.pool.get_arq_pool", return_value=FakeRedis()):
        await _on_content_flushed("doc", "test-pending-task", "proj1")

    calls = enqueue_recorder.calls
    assert len(calls) == 1
    assert calls[0][0] == "embed_document_task"
    assert calls[0][2]["job_id"] == "embed:test-pending-task"
