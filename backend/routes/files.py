"""File upload, download, and transcription status routes.
# SYSTEM: files — upload, download, transcription, markdown import with image extraction
"""
# ARCH: Magic-byte validation — every upload is checked against MAGIC_SIGNATURES
# before storage. Prevents MIME spoofing (e.g. uploading a script as image/png).

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from uuid import uuid4

import settings
from cascade import _cascade_delete_document
from collab.events import merge_live_content
from documents.service import (
    create_reference_row,
    find_reference_by_idempotency_key,
    resolve_reference_host,
)
from docx_convert import WEB_CONVERTER_TIMEOUT, post_docx_to_converter
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from files_service import (
    extract_and_replace_images,
    reprocess_reference,
    resolve_reference_file,
)
from files_util import (
    DOCX_MIME,
    PDF_MIME,
    detect_media_type,
    is_text_bytes,
    save_audio_upload,
    save_upload,
    validate_magic,
)
from markdown_normalize import normalize_markdown
from surrealdb import AsyncSurreal
from transcription import enqueue_transcription, transcribe_audio

import event_bus
from access import (
    require_document_full,
    require_document_read,
    require_project_full,
)
from auth import get_current_user
from config import (
    AUDIO_MIMES,
    STORAGE_PATH,
)
from db import extract_id, fetch_one, get_db, serialize_record
from event_bus import emit
from jobs import pool as jobs_pool
from models import is_ref_row
from routes.files_serve import _serve_reference_file, _serve_thumbnail

router = APIRouter()

logger = logging.getLogger(__name__)


async def _replay_or_reraise(project_id: str, idempotency_key: str | None) -> dict | None:
    """Catch-arm of the idempotent create: re-select by key after ANY create
    failure. Hit = the unique-index race loser — the winner's row is served as
    a replay-hit; miss = the original error stands (re-raised by the caller).
    No SurrealDB error-string matching: ANY failure takes this path. The
    loser's bytes already moved into ITS ref dir before the row create —
    orphan dir is pre-existing failure-path behavior, accepted.
    """
    if not idempotency_key:
        return None
    return await find_reference_by_idempotency_key(project_id, idempotency_key)


async def _upload_audio_reference(
    file: UploadFile, mime: str, original_name: str,
    project_id: str, document_id: str | None, title: str,
    user: dict, idempotency_key: str | None,
) -> dict:
    """Audio arm of /api/references/upload: stream to disk, create the ref,
    enqueue transcription (replay- and race-safe)."""
    try:
        ref_id, result, _ = await save_audio_upload(
            file, mime, original_name, project_id, document_id,
            title=title, processing_status="queued",
            created_by=user.get("user_id"), created_by_name=user.get("name"),
            idempotency_key=idempotency_key,
        )
    except Exception:
        existing = await _replay_or_reraise(project_id, idempotency_key)
        if existing:
            return serialize_record(existing, "reference_id")
        raise
    if await settings.get("STT_API_URL"):
        await enqueue_transcription(user.get("user_id", ""), ref_id)
    return result


async def _upload_image_reference(
    data: bytes, mime: str, original_name: str,
    project_id: str, document_id: str | None, title: str,
    user: dict, idempotency_key: str | None,
) -> dict:
    """Image arm of /api/references/upload (replay- and race-safe)."""
    try:
        _, result = await save_upload(
            data, mime, original_name, project_id, document_id,
            title=title, media_type="image", processing_status=None,
            created_by=user.get("user_id"), created_by_name=user.get("name"),
            idempotency_key=idempotency_key,
        )
    except Exception:
        existing = await _replay_or_reraise(project_id, idempotency_key)
        if existing:
            return serialize_record(existing, "reference_id")
        raise
    return result


@router.post("/api/references/upload")
@router.post("/api/documents/upload", include_in_schema=False)
async def upload_reference_file(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    title: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Upload a binary file (audio/image) and create a reference for it.

    idempotency_key (SYSTEM: recording-cache): a replay of an already-served key
    returns the EXISTING reference — no re-save, no re-enqueued transcription.
    """
    await require_project_full(project_id, user)

    # ARCH: SELECT-before-create replay guard. A lost 2xx makes the client
    # re-send the same take; without this check every retry would duplicate the
    # reference row. The unique index idx_documents_idem backs the simultaneous
    # window this SELECT cannot see; _replay_or_reraise serves the loser.
    if idempotency_key:
        existing = await find_reference_by_idempotency_key(project_id, idempotency_key)
        if existing:
            return serialize_record(existing, "reference_id")

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    media_type = detect_media_type(mime)
    if not media_type:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {mime}")

    original_name = file.filename or f"upload.{mime.split('/')[-1]}"

    if media_type == "audio":
        return await _upload_audio_reference(
            file, mime, original_name, project_id, document_id,
            title or original_name, user, idempotency_key,
        )

    max_image_mb = await settings.get("MAX_IMAGE_SIZE_MB")
    data = await file.read()
    if len(data) > max_image_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_image_mb}MB)")
    if not validate_magic(data, mime):
        raise HTTPException(status_code=400, detail="File content does not match declared type")

    return await _upload_image_reference(
        data, mime, original_name, project_id, document_id,
        title or original_name, user, idempotency_key,
    )


async def _transcribe_and_store(ref_id: str, file_path, project_id: str) -> str:
    """Transcribe a stored audio ref, persist + broadcast, schedule the embed.

    The single synchronous-write tail of upload_and_transcribe (extracted for
    the function-length gate). Returns the transcript text.
    """
    text = await transcribe_audio(file_path)
    from ydoc_store import set_content
    await set_content(ref_id, text, persist=True, extra_sets={"processing_status": "ready"})
    from collab.events import apply_external_content_change
    await apply_external_content_change("doc", ref_id, text, already_persisted=True)
    # WHY: a synchronous post-create content write emits content_flushed
    # at the write site. Why: apply_external_content_change deliberately
    # suppresses the flush for already_persisted writes (an earlier fix for a
    # spurious double flush), so without this emit the transcript is persisted
    # and broadcast but never embedded — the exact gap between the audio refs
    # that ARE embedded (worker path, jobs/tasks.py) and the ones that are not
    # (this path). Mirrors the worker path's emit shape; the debounce collapses
    # any duplicate a live editor's flush adds.
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=ref_id,
                         project_id=project_id)
    return text


async def _existing_or_new_audio_ref(
    file: UploadFile, mime: str, original_name: str,
    project_id: str, document_id: str | None,
    user: dict, idempotency_key: str | None,
) -> tuple[str, Path] | dict:
    """Replay-aware audio-ref resolution for upload_and_transcribe.

    Returns (ref_id, stored_file_path) to transcribe — either a freshly saved
    ref or the EXISTING ref of a non-ready key replay (transcription retried on
    it, no new row) — or a ready-response dict {"text", "reference_id"} when
    the replay hit an already-ready reference.
    """
    existing: dict | None = None
    if idempotency_key:
        existing = await find_reference_by_idempotency_key(project_id, idempotency_key)

    ref_id: str | None = None
    file_path: Path | None = None
    if not existing:
        try:
            ref_id, _, file_path = await save_audio_upload(
                file, mime, original_name, project_id, document_id,
                title=original_name, processing_status="processing",
                created_by=user.get("user_id"), created_by_name=user.get("name"),
                idempotency_key=idempotency_key,
            )
        except Exception:
            # Unique-index race loser with a key: fall through to the
            # existing-reference flow. Miss → the original error stands.
            existing = await _replay_or_reraise(project_id, idempotency_key)
            if not existing:
                raise

    if existing:
        ref_id = extract_id(existing["id"])
        if existing.get("processing_status") == "ready":
            return {"text": existing.get("content") or "", "reference_id": ref_id}
        file_path = STORAGE_PATH / existing["file_path"]
    return ref_id, file_path


@router.post("/api/references/upload-and-transcribe")
@router.post("/api/documents/upload-and-transcribe", include_in_schema=False)
async def upload_and_transcribe(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    idempotency_key: str | None = Form(None),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Upload audio and return transcription synchronously.

    Creates a reference as side-effect. Used by the in-document voice widget
    where polling is impractical. With idempotency_key (SYSTEM:
    recording-cache): a replay returns the stored text of the existing
    reference when it is ready, and RETRIES the transcription ON the existing
    reference (no new row) when it is not.
    """
    await require_project_full(project_id, user)

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if mime not in AUDIO_MIMES:
        raise HTTPException(status_code=400, detail=f"Only audio files accepted, got: {mime}")

    original_name = file.filename or f"voice.{mime.split('/')[-1]}"
    resolved = await _existing_or_new_audio_ref(
        file, mime, original_name, project_id, document_id, user, idempotency_key,
    )
    if isinstance(resolved, dict):
        return resolved
    ref_id, file_path = resolved

    try:
        text = await _transcribe_and_store(ref_id, file_path, project_id)
        return {"text": text, "reference_id": ref_id}
    except Exception:
        await db.query(
            "UPDATE type::record('documents', $id) SET processing_status = 'error', updated_at = time::now()",
            {"id": ref_id},
        )
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=502,
            content={"detail": "Transcription failed", "reference_id": ref_id},
        )


@router.get("/api/files/{reference_id}/thumb")
async def serve_thumbnail(reference_id: str, user: dict = Depends(get_current_user)):
    """Serve WebP thumbnail for an image reference, generating on demand if missing."""
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    await require_document_read(reference_id, user)
    return await _serve_thumbnail(ref, reference_id)


@router.get("/api/files/{reference_id}/{filename}")
async def serve_file(reference_id: str, filename: str, user: dict = Depends(get_current_user)):
    """Serve a stored reference file (authenticated)."""
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    await require_document_read(reference_id, user)
    return await _serve_reference_file(ref, filename)


@router.get("/api/mcp/download/{token}")
async def serve_mcp_download(token: str):
    """Serve a reference's bytes by signed download token (UNAUTHENTICATED).

    # The only route this plan adds that
    # serves bytes without a session. The token (minted by the authenticated
    # download_reference_file tool) binds exactly one ref_id and expires; it IS the
    # authorization. resolve_reference_file is called with project_id=None — the
    # project check ran at mint time under per-call auth; here the token replaces
    # it (is_reference + containment + exists still enforced).

    # INVARIANT (security): a valid token is the ONLY way to reach a file here, and
    # it cannot be widened (single ref_id, short exp). A bad/expired/tampered token
    # is a uniform 403 so a probe learns only "not valid".

    # ARCH(no-rate-limit): like the public-share byte routes, this unauthenticated
    # surface has NO app-layer rate limit by design — flood protection (a burst of
    # random-token GETs each costing a jwt.decode) is the reverse proxy's job.
    # Blast radius is bounded: a token is single-ref_id + short-lived, and a valid
    # hit serves an immutable write-once binary. Do NOT add a per-key limiter here
    # (there is no key) — rate-limit `/api/mcp/download/*` at the edge in prod.
    """
    from mcp_gateway.download import verify_download_token

    ref_id = verify_download_token(token)
    abs_path, mime, safe_name = await resolve_reference_file(
        ref_id=ref_id, project_id=None,
    )
    return FileResponse(
        abs_path, media_type=mime, filename=safe_name,
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.delete("/api/references/{reference_id}/file")
@router.delete("/api/documents/{reference_id}/file", include_in_schema=False)
async def delete_reference_file(reference_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Delete the binary file from a reference-document, keeping its text.

    A reference left with no text at all is deleted whole (`reference_deleted`
    in the response tells the caller which of the two happened).
    """
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    await require_document_full(reference_id, user)

    rel_path = ref.get("file_path", "")
    if rel_path:
        abs_path = STORAGE_PATH / rel_path
        if abs_path.is_file():
            abs_path.unlink()
        ref_dir = abs_path.parent
        thumb_path = ref_dir / "_thumb.webp"
        if thumb_path.is_file():
            thumb_path.unlink()
        if ref_dir.is_dir() and not any(ref_dir.iterdir()):
            ref_dir.rmdir()

    pid = ref.get("project_id", "")
    # INVARIANT(data-loss): a reference with neither file nor text is deleted whole;
    # the text is read from the live collab session first.
    # Why: user ruling — deleting the picture from the reference view must not leave
    # an empty text reference behind; reading only the DB row would delete text
    # typed in the open editor before its ~1s flush.
    if not (merge_live_content(ref, reference_id).get("content") or "").strip():
        await _cascade_delete_document(db, reference_id)
        await emit("entity_deleted", entity_type="doc", entity_id=reference_id)
        await emit("reference_deleted", project_id=pid, reference_id=reference_id)
        return {"success": True, "reference_deleted": True}

    # INVARIANT(persisted): a ref that keeps its text ALWAYS lands on markdown. Why: a
    # file-less image/audio/file ref that kept its media_type would render blank
    # (no media surface left; the editor branch treats image as non-editor), and the
    # frontend's optimistic patch already assumes markdown.
    await db.query(
        "UPDATE type::record('documents', $id) SET file_path = NONE, file_meta = NONE, "
        "processing_status = NONE, media_type = 'markdown', updated_at = time::now()",
        {"id": reference_id},
    )
    await emit("reference_updated", project_id=pid, reference_id=reference_id)
    return {"success": True, "reference_deleted": False}


@router.get("/api/references/{reference_id}/status")
@router.get("/api/documents/{reference_id}/status", include_in_schema=False)
async def get_reference_status(reference_id: str, user: dict = Depends(get_current_user)):
    """Poll processing status of a reference-document."""
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    await require_document_read(reference_id, user)
    return {
        "processing_status": ref.get("processing_status"),
        "content": ref.get("content", ""),
        "media_type": ref.get("media_type", "markdown"),
    }


@router.post("/api/references/{reference_id}/retry")
@router.post("/api/documents/{reference_id}/retry", include_in_schema=False)
async def retry_processing(reference_id: str, user: dict = Depends(get_current_user)):
    """Re-queue a stuck/failed reference: audio → transcription, .docx → conversion.

    Delegates to the shared `_reprocess_reference` core (also used by the agent-only
    reprocess_reference tool). Access (require_document_full) is checked at this
    edge; the core owns the retryable-state + dispatch-by-file-type invariants.
    """
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    await require_document_full(reference_id, user)
    return await reprocess_reference(reference_id, ref, user_id=user.get("user_id", ""))


@router.post("/api/documents/extract-text")
async def extract_text_for_insert(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Synchronously extract cleaned Markdown from a dropped file (editor file-drop).

    Mirrors the cleaning choke points of `upload-markdown` / `convert_docx_task`
    but returns the Markdown immediately for inline insertion — NO parent
    reference record is created (only image-reference records from
    `extract_and_replace_images`). The .docx converter is stateless/pure, so it is
    safe to call synchronously from the web process under a short timeout.

    # ARCH: docx conversion in the web process — mitigated by MAX_DOCX_SIZE_MB +
    # a 60s timeout (WEB_CONVERTER_TIMEOUT). Large batch imports still go through
    # the async reference flow (upload-docx → convert_docx_task on the worker).
    """
    await require_project_full(project_id, user)

    data = await file.read()
    filename = file.filename or "Untitled"

    if validate_magic(data, DOCX_MIME):
        max_docx_mb = await settings.get("MAX_DOCX_SIZE_MB")
        if len(data) > max_docx_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_docx_mb}MB)")
        try:
            markdown = await post_docx_to_converter(
                data, filename, timeout=WEB_CONVERTER_TIMEOUT,
            )
        except (RuntimeError, asyncio.TimeoutError) as e:
            logger.warning("extract-text docx conversion failed: %s", e)
            raise HTTPException(status_code=502, detail="Document conversion failed")
        content = normalize_markdown(markdown)
    else:
        max_md_mb = await settings.get("MAX_MARKDOWN_SIZE_MB")
        if len(data) > max_md_mb * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"File too large (max {max_md_mb}MB)")
        content = is_text_bytes(data)
        if content is None:
            raise HTTPException(status_code=400, detail="File content is not text or unsupported")
        # INVARIANT: normalize runs only for .md/.markdown — reflowing arbitrary
        # .txt/code/verse would join meaningful line breaks. Mirrors upload-markdown.  Why: reflow joins hard line breaks; running it on .txt/code/verse would destroy meaningful breaks, so it's gated to markdown only.
        if filename.endswith(".md") or filename.endswith(".markdown"):
            content = normalize_markdown(content)

    try:
        processed, image_refs = await extract_and_replace_images(
            content, project_id, document_id,
            created_by=user.get("user_id"), created_by_name=user.get("name"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"markdown": processed, "image_references": image_refs}


@router.post("/api/references/upload-markdown")
@router.post("/api/documents/upload-markdown", include_in_schema=False)
async def upload_markdown_reference(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    title: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Upload any UTF-8 text file as a reference, extracting base64 images as image refs.

    Acceptance is by CONTENT, not extension (is_text_bytes): a .txt, source file, etc. is
    fine. Markdown-specific normalization runs only for .md/.markdown so reflow does not
    mangle code or verse in arbitrary text.
    """
    await require_project_full(project_id, user)

    raw_bytes = await file.read()
    max_md_mb = await settings.get("MAX_MARKDOWN_SIZE_MB")
    if len(raw_bytes) > max_md_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_md_mb}MB)")
    content = is_text_bytes(raw_bytes)
    if content is None:
        raise HTTPException(status_code=400, detail="File content is not text")

    filename = file.filename or "Untitled"
    ref_title = title or os.path.splitext(os.path.basename(filename))[0] or "Untitled"

    # see SYSTEM: markdown content normalize — .md upload entry point (see markdown_normalize).
    # INVARIANT: normalize (dedent list-nested fences, reflow wrapped prose, collapse
    # post-marker spacing) only for Markdown files. Why: reflowing arbitrary
    # .txt/code/verse would join meaningful line breaks. Runs BEFORE image extraction so
    # the image regexes see clean content.
    if filename.endswith(".md") or filename.endswith(".markdown"):
        content = normalize_markdown(content)

    try:
        processed_content, ref_records = await extract_and_replace_images(
            content, project_id, document_id,
            created_by=user.get("user_id"), created_by_name=user.get("name"),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    ref_id = str(uuid4())
    # "project level" markdown upload (no host selected) → host on the project index
    # doc. INVARIANT: paired with the documents_reference_parent_check schema event.  Why: the app-level host write is backed by a schema event so the parent invariant can't be bypassed by a stray write — enforcement sits at the DB layer.
    host_id = await resolve_reference_host(document_id, project_id)
    record = await create_reference_row(
        ref_id=ref_id, project_id=project_id, host_id=host_id,
        title=ref_title, media_type="markdown",
        content=processed_content, source_url="",
        created_by=user.get("user_id"), created_by_name=user.get("name"),
    )

    md_ref = serialize_record(record, "reference_id")
    return {"reference": md_ref, "image_references": ref_records}


@router.post("/api/references/upload-docx")
@router.post("/api/documents/upload-docx", include_in_schema=False)
async def upload_docx_reference(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    title: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Upload a .docx, create a queued markdown reference, and enqueue conversion.

    Conversion (Pandoc, images→refs, formulas→LaTeX) runs async in convert_docx_task;
    the client gets a queued reference immediately and polls /status (or receives the
    reference_status_changed WS event). The raw .docx is stored so retries can re-run.
    """
    await require_project_full(project_id, user)

    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="File must have a .docx extension")

    data = await file.read()
    max_docx_mb = await settings.get("MAX_DOCX_SIZE_MB")
    if len(data) > max_docx_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_docx_mb}MB)")
    if not validate_magic(data, DOCX_MIME):
        raise HTTPException(status_code=400, detail="File content is not a valid .docx")

    ref_title = title or file.filename
    ref_id, result = await save_upload(
        data, DOCX_MIME, file.filename, project_id, document_id,
        title=ref_title, media_type="markdown", processing_status="queued",
        created_by=user.get("user_id"), created_by_name=user.get("name"),
    )

    # The uploader rides the task so extracted images are attributed too
    # (create_reference_row callers must pass what they have — plan rule).
    await jobs_pool.enqueue("convert_docx_task", ref_id, user.get("user_id", ""),
                  user.get("name"), job_id=f"docx:{ref_id}")

    return result


@router.post("/api/references/upload-pdf")
@router.post("/api/documents/upload-pdf", include_in_schema=False)
async def upload_pdf_reference(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    document_id: str | None = Form(None),
    title: str | None = Form(None),
    user: dict = Depends(get_current_user),
):
    """Upload a .pdf, create a queued markdown reference, and enqueue conversion.

    Mirrors upload_docx_reference end to end: conversion (pymupdf4llm in the same
    converter container, images→refs, min-side + dedup filters there) runs async
    in convert_pdf_task; the client gets a queued reference immediately and polls
    /status (or receives the reference_status_changed WS event). The raw .pdf is
    stored so retries can re-run. Own size cap: MAX_PDF_SIZE_MB, not the docx one.
    """
    await require_project_full(project_id, user)

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must have a .pdf extension")

    data = await file.read()
    max_pdf_mb = await settings.get("MAX_PDF_SIZE_MB")
    if len(data) > max_pdf_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large (max {max_pdf_mb}MB)")
    if not validate_magic(data, PDF_MIME):
        raise HTTPException(status_code=400, detail="File content is not a valid .pdf")

    ref_title = title or file.filename
    ref_id, result = await save_upload(
        data, PDF_MIME, file.filename, project_id, document_id,
        title=ref_title, media_type="markdown", processing_status="queued",
        created_by=user.get("user_id"), created_by_name=user.get("name"),
    )

    # The uploader rides the task so extracted images are attributed too
    # (create_reference_row callers must pass what they have — plan rule).
    await jobs_pool.enqueue("convert_pdf_task", ref_id, user.get("user_id", ""),
                  user.get("name"), job_id=f"pdf:{ref_id}")

    return result
