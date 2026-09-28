"""Tests for PDF import: upload-pdf endpoint, convert_pdf_task, worker wiring, magic.

Mirrors test_docx_import.py: the upload endpoint saves the .pdf + creates a queued
markdown reference + enqueues convert_pdf_task on the default worker queue. The
task body is shared with convert_docx_task (_convert_upload_task) — the converter
branches on the file extension internally — so these tests pin the PDF-specific
seams: the endpoint contract (job_id pdf:{id}), the %PDF- magic, the worker
registration, and the blank-markdown guard (a scanned PDF with no text layer
must land error + reference_status_changed, not ready+empty — silent degradation).
"""

import asyncio
import io
from unittest.mock import AsyncMock, patch

import pytest
from emit_recorder import EmitRecorder

# Minimal valid PDF magic: the %PDF- header signature.
PDF_MAGIC = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64

# 1x1 PNG, base64 — the PDF converter emits labelled definitions + usages
# (`[label]: data:...` / `![alt][label]`), unlike DOCX's inline `![](data:...)`.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
MARKDOWN_WITH_IMAGE = (
    "# Title\n\nSome body text.\n\n"
    f"[img-abc123]: data:image/png;base64,{_PNG_B64}\n\n"
    "![|560x428][img-abc123]\n"
)


async def _create_doc(client, token, pid, title="PdfDoc"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


# ---- magic-byte validation ----

def test_validate_magic_accepts_pdf_header():
    from files_util import PDF_MIME, validate_magic

    assert validate_magic(PDF_MAGIC, PDF_MIME) is True


def test_validate_magic_rejects_non_pdf_bytes():
    from files_util import PDF_MIME, validate_magic

    assert validate_magic(b"not a pdf at all", PDF_MIME) is False


# ---- endpoint ----

@pytest.mark.asyncio
async def test_upload_pdf_creates_queued_markdown_ref_and_enqueues(client, admin_user, project_with_doc, enqueue_recorder):
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc_id = await _create_doc(client, token, pid)

    resp = await client.post(
        "/api/references/upload-pdf",
        data={"project_id": pid, "document_id": doc_id, "title": "paper.pdf"},
        files={"file": ("paper.pdf", io.BytesIO(PDF_MAGIC), "application/pdf")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    ref_id = data["reference_id"]
    assert data["media_type"] == "markdown"
    assert data["processing_status"] == "queued"

    calls = enqueue_recorder.of("convert_pdf_task")
    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "convert_pdf_task"
    assert args[0] == ref_id
    # The uploader rides the task so images extracted from the .pdf are
    # attributed to them (same rule as DOCX).
    assert args[1] == admin_uid
    assert args[2] == "testadmin"
    assert kwargs.get("job_id") == f"pdf:{ref_id}"


@pytest.mark.asyncio
async def test_upload_pdf_rejects_wrong_extension(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _create_doc(client, token, pid)
    resp = await client.post(
        "/api/references/upload-pdf",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("notes.txt", io.BytesIO(PDF_MAGIC), "text/plain")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_upload_pdf_rejects_bad_magic(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_id = await _create_doc(client, token, pid)
    resp = await client.post(
        "/api/references/upload-pdf",
        data={"project_id": pid, "document_id": doc_id},
        files={"file": ("paper.pdf", io.BytesIO(b"this is not a pdf"), "application/pdf")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 400


# ---- arq task ----

async def _create_pdf_ref(ref_id: str, pid: str, doc_id: str = "owner-doc") -> None:
    from config import STORAGE_PATH
    from db import create_record

    rel_path = f"{pid}/{ref_id}/paper.pdf"
    abs_path = STORAGE_PATH / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(PDF_MAGIC)

    await create_record("documents", ref_id, {
        "project_id": pid,
        "parent_id": doc_id,
        "title": "paper.pdf",
        "media_type": "markdown",
        "content": "",
        "processing_status": "queued",
        "file_path": rel_path,
        "path": f"_ref/{ref_id}.md",
        "is_reference": True,
    })


@pytest.mark.asyncio
async def test_convert_pdf_task_success_extracts_images_and_sets_ready(test_db):
    """The shared body collapses the PDF form (labelled def + usage) into ONE
    image reference — same extract_and_replace_images path as DOCX."""
    from jobs.tasks import convert_pdf_task

    ref_id = "pdf-task-ok"
    pid = "pdf-task-project"
    await _create_pdf_ref(ref_id, pid)

    with patch("jobs.tasks.media._post_to_converter", new_callable=AsyncMock, return_value=MARKDOWN_WITH_IMAGE), \
         patch("ydoc_store.set_content", new_callable=AsyncMock) as set_content:
        await convert_pdf_task({"job_try": 1, "max_tries": 2}, ref_id, "user-1")
    set_content.assert_awaited_once()

    processed = set_content.await_args.args[1]
    # Labelled definition collapsed into a ref: link, base64 no longer present.
    assert "data:image/png;base64" not in processed
    assert "(ref:" in processed
    assert set_content.await_args.kwargs["extra_sets"]["processing_status"] == "ready"


@pytest.mark.asyncio
async def test_convert_pdf_task_blank_markdown_marks_error_not_ready(test_db):
    """A PDF with no text layer (pure scan) converts to EMPTY markdown — that
    must land error + reference_status_changed, not a green ready with empty
    content (silent degradation, plan Risks)."""
    from db import fetch_one
    from jobs.tasks import convert_pdf_task

    ref_id = "pdf-task-blank"
    pid = "pdf-task-project-blank"
    await _create_pdf_ref(ref_id, pid)

    with patch("jobs.tasks.media._post_to_converter", new_callable=AsyncMock, return_value="  \n\n"), \
         patch("ydoc_store.set_content", new_callable=AsyncMock) as set_content, \
         EmitRecorder.active() as emit:
        # Expected-failure shape: NOT an exception (no dead-letter) — the guard
        # owns the exit.
        await convert_pdf_task({"job_try": 1, "max_tries": 2}, ref_id, "user-1")
        await asyncio.sleep(0.05)

    set_content.assert_not_awaited()
    ref = await fetch_one("documents", ref_id)
    assert ref["processing_status"] == "error"
    assert ("reference_status_changed", {
        "reference_id": ref_id, "project_id": pid, "status": "error",
    }) in emit.calls


@pytest.mark.asyncio
async def test_convert_pdf_task_error_sets_error_status_and_raises(test_db):
    from db import fetch_one
    from jobs.tasks import convert_pdf_task

    ref_id = "pdf-task-err"
    pid = "pdf-task-project-err"
    await _create_pdf_ref(ref_id, pid)

    with patch("jobs.tasks.media._post_to_converter", new_callable=AsyncMock, side_effect=RuntimeError("pymupdf failed")):
        with pytest.raises(RuntimeError):
            await convert_pdf_task({"job_try": 1, "max_tries": 2}, ref_id, "user-1")
        await asyncio.sleep(0.05)

    ref = await fetch_one("documents", ref_id)
    assert ref["processing_status"] == "error"


# ---- worker registration ----

def test_convert_pdf_task_registered_on_default_worker():
    from jobs.worker import TranscriptionWorkerSettings, WorkerSettings

    default_names = {getattr(f, "name", None) for f in WorkerSettings.functions}
    trans_names = {getattr(f, "name", None) for f in TranscriptionWorkerSettings.functions}
    assert "convert_pdf_task" in default_names
    # Like DOCX: conversion is offloaded to the converter container, so the task
    # stays on the default queue, not the STT-bounded transcription worker.
    assert "convert_pdf_task" not in trans_names
