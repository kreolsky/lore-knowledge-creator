"""Tests for the shared reprocess core + the Pi-only reprocess_reference tool.

Context (plan agent-reference-text-is-canon): a reference's TEXT is canon — the
import pipeline produced it. The sanctioned way to re-run that pipeline is the
Pi-only `reprocess_reference` tool, NOT re-deriving text from the binary in the
sandbox (no ASR/OCR/converters there). This file pins:

  - the shared core extracted from routes.files.retry_processing (audio →
    re-transcribe, .docx → re-convert, wipe content, queue the job);
  - the image refusal (look at the image / ask the user — never OCR);
  - the 'ready'-state handling (Decision 7 = A: accepted under the apply-mode
    gate, no special refusal — we cannot detect hand-editing);
  - the tool surface: confirm proposes (no wipe), auto applies; a non-full
    principal (Commentator/Viewer) is refused (content-destroying write).

The core lives in routes.files (retry_processing's home); the tool handler in
routes.tool_api.imports reuses it. Access checks stay at each surface's own edge
(require_document_full for REST, project access for the tool) — they are NOT
unified, by design.
"""

import secrets
from unittest.mock import AsyncMock, patch

import pytest
from enqueue_recorder import EnqueueRecorder
from helpers import pinned_stt_url

# ─── Shared seeding helpers ──────────────────────────────────────────────────


async def _seed_ref(
    test_db, project_id, ref_id, *, media_type, file_path=None,
    processing_status="error", content="OLD TEXT", parent_id=None,
    safe_name="clip.mp3",
):
    """Persist a reference row mirroring save_upload (no disk bytes needed — the
    core never reads the file, it only dispatches on media_type/file_path)."""
    from db import create_record

    if parent_id is None:
        # Reference-host invariant: attach to a real host doc (idempotent per project).
        parent_id = f"{project_id}-host"
        try:
            await create_record("documents", parent_id, {
                "project_id": project_id, "parent_id": None, "title": "Host",
                "content": "", "path": f"{parent_id}.md", "is_index": False,
            })
        except RuntimeError:
            pass
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": parent_id, "title": safe_name,
        "content": content, "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": media_type,
        "file_path": file_path, "processing_status": processing_status,
    })
    return ref_id


async def _fetch_ref(ref_id):
    from db import fetch_one

    return await fetch_one("documents", ref_id)


# ════════════════════════════════════════════════════════════════════════════
# Shared core (routes.files._reprocess_reference) — extracted from retry_processing
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_core_audio_wipes_content_and_enqueues_transcription(test_db):
    """Audio ref → content wiped, processing_status='queued', enqueue_transcription
    called (when STT is configured). Mirrors retry_processing's audio dispatch."""
    from files_service import reprocess_reference

    pid = "rp-core-audio"
    ref_id = "rp-ref-audio"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="error")
    ref = await _fetch_ref(ref_id)

    with pinned_stt_url(), \
            patch("files_service.enqueue_transcription", new_callable=AsyncMock) as enc:
        result = await reprocess_reference(ref_id, ref, user_id="u1")

    assert result["success"] is True
    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"
    enc.assert_awaited_once()
    # enqueue_transcription(user_id, reference_id) — second positional is the ref.
    assert enc.await_args.args[-1] == ref_id


@pytest.mark.asyncio
async def test_core_docx_wipes_content_and_enqueues_convert_task(test_db):
    """A markdown reference whose source file is a .docx → re-convert. Dispatch is
    by the stored file type, not a generic flag (the retry INVARIANT)."""
    from files_service import reprocess_reference

    pid = "rp-core-docx"
    ref_id = "rp-ref-docx"
    await _seed_ref(test_db, pid, ref_id, media_type="markdown",
                    file_path=f"{pid}/{ref_id}/notes.docx", processing_status="error")
    ref = await _fetch_ref(ref_id)

    with EnqueueRecorder.active() as enq:
        result = await reprocess_reference(ref_id, ref, user_id="u1")

    assert result["success"] is True
    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"
    # enqueue("convert_docx_task", reference_id, user_id, job_id=f"docx:{ref_id}")
    called_task = enq.of("convert_docx_task")
    assert len(called_task) == 1
    assert called_task[0].args[0] == ref_id


@pytest.mark.asyncio
async def test_core_pdf_wipes_content_and_enqueues_convert_pdf_task(test_db):
    """A markdown reference whose source file is a .pdf → re-convert via
    convert_pdf_task (job_id pdf:{id}) — the retry gate is per-extension
    (is_convertible_reference), mirroring the DOCX dispatch."""
    from files_service import reprocess_reference

    pid = "rp-core-pdf"
    ref_id = "rp-ref-pdf"
    await _seed_ref(test_db, pid, ref_id, media_type="markdown",
                    file_path=f"{pid}/{ref_id}/paper.pdf", processing_status="error",
                    safe_name="paper.pdf")
    ref = await _fetch_ref(ref_id)

    with EnqueueRecorder.active() as enq:
        result = await reprocess_reference(ref_id, ref, user_id="u1")

    assert result["success"] is True
    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"
    # enqueue("convert_pdf_task", reference_id, user_id, job_id=f"pdf:{ref_id}")
    called_task = enq.of("convert_pdf_task")
    assert len(called_task) == 1
    assert called_task[0].args[0] == ref_id
    assert called_task[0].kwargs.get("job_id") == f"pdf:{ref_id}"


@pytest.mark.asyncio
async def test_core_image_refused_naming_the_alternative(test_db):
    """Images are never reprocessed — no OCR pipeline exists. The refusal must name
    the alternative (look at the image / ask the user), and must NOT wipe content."""
    from fastapi import HTTPException
    from files_service import reprocess_reference

    pid = "rp-core-img"
    ref_id = "rp-ref-img"
    await _seed_ref(test_db, pid, ref_id, media_type="image",
                    file_path=f"{pid}/{ref_id}/pic.png", processing_status=None,
                    safe_name="pic.png")
    ref = await _fetch_ref(ref_id)

    with pytest.raises(HTTPException) as ei:
        await reprocess_reference(ref_id, ref, user_id="u1")
    assert ei.value.status_code == 400
    detail = ei.value.detail.lower()
    assert "image" in detail
    # Names the alternative, not just "cannot".
    assert "look" in detail or "ask" in detail
    # Content untouched.
    after = await _fetch_ref(ref_id)
    assert after["content"] == "OLD TEXT"


@pytest.mark.asyncio
async def test_core_ready_state_accepted_under_gate(test_db):
    """Decision 7 = A: a 'ready' reference (possibly hand-cleaned) IS reprocessable.
    We cannot detect hand-editing (no stored baseline), and the apply-mode gate /
    confirmation is the protection — so the core accepts it and wipes on execution.
    Asserts the CHOSEN behaviour, not silence."""
    from files_service import reprocess_reference

    pid = "rp-core-ready"
    ref_id = "rp-ref-ready"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="ready",
                    content="HAND-CLEANED TRANSCRIPT")
    ref = await _fetch_ref(ref_id)

    with pinned_stt_url(), \
            patch("files_service.enqueue_transcription", new_callable=AsyncMock):
        await reprocess_reference(ref_id, ref, user_id="u1")

    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"


@pytest.mark.asyncio
async def test_core_non_retryable_status_refused(test_db):
    """Only error/ready/processing are retryable (the retry INVARIANT — 'processing'
    is included so a user can self-unstick a frozen ref). A 'queued' ref is refused,
    and its content is NOT wiped."""
    from fastapi import HTTPException
    from files_service import reprocess_reference

    pid = "rp-core-status"
    ref_id = "rp-ref-status"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="queued")
    ref = await _fetch_ref(ref_id)

    with pytest.raises(HTTPException) as ei:
        await reprocess_reference(ref_id, ref, user_id="u1")
    assert ei.value.status_code == 400
    after = await _fetch_ref(ref_id)
    assert after["content"] == "OLD TEXT"


@pytest.mark.asyncio
async def test_core_no_file_path_refused(test_db):
    """A reference with no attached binary cannot be re-processed — refuse, do not
    wipe."""
    from fastapi import HTTPException
    from files_service import reprocess_reference

    pid = "rp-core-nofile"
    ref_id = "rp-ref-nofile"
    await _seed_ref(test_db, pid, ref_id, media_type="audio", file_path=None)
    ref = await _fetch_ref(ref_id)

    with pytest.raises(HTTPException) as ei:
        await reprocess_reference(ref_id, ref, user_id="u1")
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_core_unsupported_source_type_refused(test_db):
    """A markdown reference whose source file is NOT a .docx (e.g. a .txt) cannot be
    re-processed by the pipeline — refuse."""
    from fastapi import HTTPException
    from files_service import reprocess_reference

    pid = "rp-core-txt"
    ref_id = "rp-ref-txt"
    await _seed_ref(test_db, pid, ref_id, media_type="markdown",
                    file_path=f"{pid}/{ref_id}/notes.txt", processing_status="error",
                    safe_name="notes.txt")
    ref = await _fetch_ref(ref_id)

    with pytest.raises(HTTPException) as ei:
        await reprocess_reference(ref_id, ref, user_id="u1")
    assert ei.value.status_code == 400


# ════════════════════════════════════════════════════════════════════════════
# Tool surface (POST /api/tool/reprocess_reference) — Pi-only
# ════════════════════════════════════════════════════════════════════════════


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    import hashlib

    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"rp-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_tool_auto_applies_and_wipes(client, test_db, admin_user, project_with_doc):
    """Full access + apply=auto → the tool runs the core directly and returns
    {status:"applied"}; the content is wiped and the job queued."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    ref_id = "rp-tool-auto"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="error")

    with pinned_stt_url(), \
            patch("files_service.enqueue_transcription", new_callable=AsyncMock):
        resp = await client.post("/api/tool/reprocess_reference", json={
            "reference_id": ref_id, "apply": "auto",
        }, headers=_hdr(agent_tok))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "applied"
    after = await _fetch_ref(ref_id)
    assert after["content"] == ""
    assert after["processing_status"] == "queued"


@pytest.mark.asyncio
async def test_tool_confirm_refuses_image_before_confirmation(
    client, test_db, admin_user, project_with_doc,
):
    """An image reference is refused immediately even in confirm mode — it can never
    be reprocessed (no OCR), so the refusal fires before the confirmation hold."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    ref_id = "rp-tool-img"
    await _seed_ref(test_db, pid, ref_id, media_type="image",
                    file_path=f"{pid}/{ref_id}/pic.png", processing_status=None,
                    safe_name="pic.png")

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": ref_id, "apply": "confirm",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 400, resp.text
    assert "image" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_tool_non_full_principal_refused(
    client, test_db, admin_user, project_with_doc,
):
    """A Commentator/Viewer key (project access != full) cannot reprocess — it is a
    content-destroying write. Assert the refusal, not the happy path."""
    from db import create_record

    pid, _, _admin_uid = project_with_doc

    # A non-owner member demoted to Commentator (the owner is always full, so the
    # refusal must be exercised through a separate principal) + their agent key.
    comm_uid = f"comm-{secrets.token_hex(4)}"
    await create_record("users", comm_uid, {
        "email": f"{comm_uid}@x.test", "name": comm_uid, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"pm-{secrets.token_hex(4)}", {
        "project_id": pid, "user_id": comm_uid, "access_level": "commentator",
    })
    agent_tok = await _make_agent_key(test_db, comm_uid, pid)
    ref_id = "rp-tool-viewer"
    await _seed_ref(test_db, pid, ref_id, media_type="audio",
                    file_path=f"{pid}/{ref_id}/clip.mp3", processing_status="error")

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": ref_id, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_tool_unknown_reference_404(client, test_db, admin_user, project_with_doc):
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": "does-not-exist", "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 404, resp.text


# ════════════════════════════════════════════════════════════════════════════
# Cross-project authorization — full access to project A must not reach project B
# (parity with the REST retry edge's require_document_full + the edit executors'
# session-project binding). The wipe is content-destroying; content must stay intact.
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tool_refuses_cross_project_reference(
    client, test_db, admin_user, project_with_doc,
):
    """A key bound to project A must NOT wipe a reference living in project B. The
    tool binds the fetched ref to ctx['project_id'] (parity with the REST retry edge,
    which gates on the ref's OWN project via require_document_full)."""
    pid, _, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)  # key for project A
    other_pid = "rp-other-project"
    ref_id = "rp-xproj"
    await _seed_ref(test_db, other_pid, ref_id, media_type="audio",
                    file_path=f"{other_pid}/{ref_id}/clip.mp3", processing_status="error")

    resp = await client.post("/api/tool/reprocess_reference", json={
        "reference_id": ref_id, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 404, resp.text
    # Uniform 404 (no existence oracle) — but the cross-project wipe never happened.
    assert (await _fetch_ref(ref_id))["content"] == "OLD TEXT"


# ════════════════════════════════════════════════════════════════════════════
# Reference-file serving — Content-Disposition is inline ONLY for PDF
# (plan pdf-import-pymupdf4llm: "open in new tab" must render in the browser's
# viewer, not download; every other binary keeps the attachment default —
# explicit is better than relying on FileResponse's default here).
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_serve_disposition_inline_for_pdf_attachment_otherwise():
    """_serve_reference_file: a PDF reference serves Content-Disposition: inline
    (browser viewer in a new tab); a DOCX reference keeps attachment. Download
    is unaffected — the frontend download link's `download` attribute wins over
    inline for a same-origin URL."""
    from routes.files_serve import _serve_reference_file

    from config import STORAGE_PATH

    d = STORAGE_PATH / "rp-serve"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "paper.pdf"
    f.write_bytes(b"%PDF-1.7\n")
    try:
        pdf_ref = {
            "is_reference": True, "project_id": "rp-serve-proj",
            "file_path": "rp-serve/paper.pdf",
            "file_meta": {"mime_type": "application/pdf"},
        }
        resp = await _serve_reference_file(pdf_ref, "paper.pdf")
        assert resp.headers["content-disposition"].startswith("inline")

        docx_ref = {
            "is_reference": True, "project_id": "rp-serve-proj",
            "file_path": "rp-serve/paper.pdf",
            "file_meta": {
                "mime_type": "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document",
            },
        }
        resp = await _serve_reference_file(docx_ref, "paper.docx")
        assert resp.headers["content-disposition"].startswith("attachment")
    finally:
        f.unlink(missing_ok=True)
        try:
            d.rmdir()
        except OSError:
            pass


