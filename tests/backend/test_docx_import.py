"""Tests for DOCX import: upload-docx endpoint, convert_docx_task, worker wiring, magic.

DOCX import mirrors the async transcription flow: the upload endpoint saves the
.docx + creates a queued markdown reference + enqueues convert_docx_task on the
default worker queue. The task offloads heavy conversion to the stateless
`converter` container (HTTP), then reuses extract_and_replace_images to store
images as refs and set_content to push the result to editors via the backplane.
"""

import asyncio
import io
from unittest.mock import AsyncMock, patch

import pytest
from emit_recorder import EmitRecorder

# Minimal valid DOCX magic: a ZIP local-file-header signature.
DOCX_MAGIC = b"PK\x03\x04" + b"\x00" * 64

# 1x1 PNG, base64 — used to verify the converter→extract_and_replace_images path.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
MARKDOWN_WITH_IMAGE = f"# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n![pic](data:image/png;base64,{_PNG_B64})\n\n$E = mc^2$\n"


async def _create_doc(client, token, pid, title="DocxDoc"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


# ---- magic-byte validation ----


# ---- endpoint ----

@pytest.mark.asyncio
async def test_upload_docx_creates_queued_markdown_ref_and_enqueues(client, admin_user, project_with_doc, enqueue_recorder):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _create_doc(client, token, pid)

    resp = await client.post(
        "/api/documents/upload-docx",
        data={"project_id": pid, "document_id": doc_id, "title": "report.docx"},
        files={"file": ("report.docx", io.BytesIO(DOCX_MAGIC),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    ref_id = data["reference_id"]
    assert data["media_type"] == "markdown"
    assert data["processing_status"] == "queued"

    assert len(enqueue_recorder.calls) == 1
    name, args, kwargs = enqueue_recorder.calls[0]
    assert name == "convert_docx_task"
    assert args[0] == ref_id
    # The uploader rides the task so images extracted from the .docx are
    # attributed to them (review fix R3).
    assert args[1] == admin_uid
    assert args[2] == "testadmin"
    assert kwargs.get("job_id") == f"docx:{ref_id}"


# ---- arq task ----

async def _create_docx_ref(ref_id: str, pid: str, doc_id: str = "owner-doc") -> None:
    from config import STORAGE_PATH
    from db import create_record

    rel_path = f"{pid}/{ref_id}/report.docx"
    abs_path = STORAGE_PATH / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(DOCX_MAGIC)

    await create_record("documents", ref_id, {
        "project_id": pid,
        "parent_id": doc_id,
        "title": "report.docx",
        "media_type": "markdown",
        "content": "",
        "processing_status": "queued",
        "file_path": rel_path,
        "path": f"_ref/{ref_id}.md",
        "is_reference": True,
    })


@pytest.mark.asyncio
async def test_convert_docx_task_emits_content_flushed_to_schedule_embed(test_db):
    """A worker-written content change MUST emit content_flushed so the embedding
    scheduler enqueues an embed (D1b). Without it, an imported .docx is persisted
    and broadcast to editors but NEVER embedded — exactly the 4-of-179-docs shape
    the plan reproduces (only editor-flushed docs were embedded).
    """
    from event_bus import off, on
    from jobs.tasks import convert_docx_task

    ref_id = "docx-task-flush"
    pid = "docx-task-project-flush"
    await _create_docx_ref(ref_id, pid)

    flushed = []
    async def capture(**kw):
        flushed.append(kw)

    on("content_flushed", capture)
    try:
        with patch("jobs.tasks.media._post_to_converter", new_callable=AsyncMock, return_value=MARKDOWN_WITH_IMAGE), \
             patch("ydoc_store.set_content", new_callable=AsyncMock):
            await convert_docx_task({"job_try": 1, "max_tries": 2}, ref_id, "user-1")
            await asyncio.sleep(0.05)  # drain fire-and-forget emit

        assert len(flushed) == 1, f"expected one content_flushed, got {flushed}"
        assert flushed[0]["entity_type"] == "doc"
        assert flushed[0]["entity_id"] == ref_id
        assert flushed[0]["project_id"] == pid
    finally:
        off("content_flushed", capture)


@pytest.mark.asyncio
async def test_convert_docx_task_missing_file_emits_error_event(test_db):
    """Early error exit (no file_path) sets 'error' AND emits the WS event — no silent
    degradation. The UI must learn of this failure the same way it learns of a converter
    error, not only on the next poll."""
    from db import create_record, fetch_one
    from jobs.tasks import convert_docx_task

    ref_id = "docx-task-nofile"
    pid = "docx-task-project-nofile"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": "owner-doc", "title": "report.docx",
        "media_type": "markdown", "content": "", "processing_status": "queued",
        "file_path": "", "path": f"_ref/{ref_id}.md", "is_reference": True,
    })

    # emit is imported lazily inside the task (from event_bus import emit), so patch the
    # source module, not jobs.tasks.
    with EmitRecorder.active() as emit:
        await convert_docx_task({"job_try": 1, "max_tries": 2}, ref_id, "user-1")

    ref = await fetch_one("documents", ref_id)
    assert ref["processing_status"] == "error"
    assert ("reference_status_changed", {
        "reference_id": ref_id, "project_id": pid, "status": "error",
    }) in emit.calls


# ---- retry endpoint dispatch ----

@pytest.mark.asyncio
async def test_retry_dispatches_docx_to_convert_task(client, admin_user, project_with_doc, enqueue_recorder):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _create_doc(client, token, pid)
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "parent_id": doc_id, "title": "DocxRetry",
              "media_type": "markdown", "is_reference": True, "content": "old"},
        cookies={"lore_session": token},
    )
    ref_id = resp.json()["document_id"]
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'error', "
        "file_path = $fp", {"id": ref_id, "fp": f"{pid}/{ref_id}/report.docx"},
    )

    resp = await client.post(f"/api/documents/{ref_id}/retry", cookies={"lore_session": token})
    assert resp.status_code == 200
    # The recorder spans the whole test, so doc-creation side effects
    # (embed_document_task) land in .calls too — narrow to the retry dispatch.
    calls = enqueue_recorder.of("convert_docx_task")
    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "convert_docx_task"
    assert args[0] == ref_id
    assert kwargs.get("job_id") == f"docx:{ref_id}"


# ---- worker registration ----
