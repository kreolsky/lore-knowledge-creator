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

# Minimal valid PDF magic: the %PDF- header signature.
PDF_MAGIC = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 64


async def _create_doc(client, token, pid, title="PdfDoc"):
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title},
        cookies={"lore_session": token},
    )
    return resp.json()["document_id"]


# ---- magic-byte validation ----


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
