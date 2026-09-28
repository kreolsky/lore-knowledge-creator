"""STT + convert media tasks — transcribe_task, convert_docx_task, convert_pdf_task.

# Media import tasks (STT/convert). see SYSTEM: jobs, SYSTEM: transcription.
# INVARIANT: the worker has no live collab session — every persisted write goes
# through ydoc_store.set_content (persists AND publishes to the backplane
# ydoc:{id} channel). Why: a plain DB UPDATE would leave every open editor
# showing stale text.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import http_clients

import event_bus
from config import STORAGE_PATH
from models import is_ref_row

logger = logging.getLogger(__name__)


async def _read_docx_bytes(abs_path: Path) -> bytes:
    """Read the stored .docx off disk (own function so the task is unit-patchable)."""
    return await asyncio.to_thread(abs_path.read_bytes)


# ARCH: the converter call lives in the shared `docx_convert` module (one
# implementation for worker + web route). This thin wrapper keeps the worker's
# long-lived pool client ("media", see SYSTEM: http-clients — the 1800s transport
# bound the worker's STT/convert budget has always used) and is the name the
# task unit tests patch.
async def _post_to_converter(data: bytes, filename: str) -> str:
    """POST a .docx to the stateless converter service, return the Markdown body.

    The converter owns no DB state — it is a pure bytes->markdown function (Pandoc
    today, swappable later). Raises on non-2xx so the caller dead-letters the ref.
    """
    from docx_convert import WORKER_CONVERTER_TIMEOUT, post_docx_to_converter

    return await post_docx_to_converter(
        data, filename,
        client=http_clients.get_http_client("media", timeout=1800),
        timeout=WORKER_CONVERTER_TIMEOUT,
    )


async def transcribe_task(ctx, user_id: str, reference_id: str) -> None:
    from transcription import transcribe_audio

    from db import fetch_one, get_db
    from jobs.tasks._dead_letter import dead_letter
    from ydoc_store import set_content

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return

    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'processing'",
        {"id": reference_id},
    )

    # INVARIANT: every error exit (missing/bad file_path AND a failed transcription)
    # notifies the UI via WS, like `_mark_error` in convert_docx_task does.
    # Why: no silent degradation — an early return that only flips the DB status
    # leaves the transcription panel waiting on a job that already failed; this
    # task's own event is transcription_error (project_ws.py handler), NOT
    # reference_status_changed.
    async def _fail_early() -> None:
        await db.query(
            "UPDATE type::record('documents', $id) SET processing_status = 'error'",
            {"id": reference_id},
        )
        await event_bus.emit("transcription_error",
                             reference_id=reference_id,
                             project_id=ref.get("project_id", ""))

    rel_path = ref.get("file_path", "")
    if not rel_path:
        await _fail_early()
        return

    abs_path = (STORAGE_PATH / rel_path).resolve()
    if not abs_path.is_relative_to(STORAGE_PATH.resolve()):
        logger.error("Path traversal attempt in ref %s: %s", reference_id, rel_path)
        await _fail_early()
        return
    if not abs_path.is_file():
        await _fail_early()
        return

    try:
        transcription = await transcribe_audio(abs_path)
    except Exception as e:
        logger.error("Transcription error for %s: %s", reference_id, e)
        # The WHEN of this dead-letter (first failure, before the re-raise) is
        # dead_letter()'s contract — see jobs.tasks.dead_letter. The recorder is
        # this task's own _fail_early (same status+event as the early exits — the
        # shape convert_docx_task's _mark_error set the precedent for).
        await dead_letter(_fail_early(), e)

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return

    # INVARIANT(corruption): Worker has no live collab session — must use set_content which
    # persists AND publishes the Yjs update to the backplane ydoc:{id} channel.
    # Why: the worker holds no editors; a plain DB UPDATE would leave every open editor
    # showing stale text. set_content is the only path that writes Surreal AND fans the
    # update to the web replica, whose live CollabSession broadcasts to editors.
    await set_content(reference_id, transcription, persist=True, extra_sets={"processing_status": "ready"})
    # set_content does NOT emit content_flushed (it is also called mid-flush by
    # paths that emit their own), so a worker write would never schedule an embed. Emit
    # here, once per worker write, the way _finalize_content_mutation does for the REST
    # path — embeddings._on_content_flushed (subscribed eagerly) then debounces.
    # Not "exactly once" globally: with a live editor open, the web replica applies this
    # write off the backplane, marks the session dirty and its flush emits its own
    # content_flushed. The trailing debounce collapses the pair into one embed, so the
    # duplicate is harmless — but do not add a second emit here on that assumption.
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=reference_id,
                         project_id=ref.get("project_id", ""))

    logger.info("Transcription done: %s", reference_id)
    await event_bus.emit("transcription_complete",
                         reference_id=reference_id,
                         project_id=ref.get("project_id", ""),
                         user_id=user_id)


async def _convert_upload_task(ctx, reference_id: str, user_id: str,
                               user_name: str | None = None) -> None:
    """Shared convert body for uploaded .docx/.pdf references → Markdown.

    Reads the stored source file, offloads conversion to the converter container
    (which branches on the file extension — pandoc vs pymupdf4llm), then reuses
    extract_and_replace_images to store embedded images as their own
    image-references (the document body links to them via ![](ref:<id>)), and
    publishes the result to editors via set_content + the Redis backplane.
    `convert_docx_task` / `convert_pdf_task` are the arq-visible wrappers — the
    names differ so the wire (enqueue/job_id) can target each format while the
    body stays ONE implementation (a rename of convert_docx_task would strand
    jobs enqueued across a deploy; see plan pdf-import-pymupdf4llm).

    user_id/user_name: the uploading user, threaded from the enqueueing route so
    the EXTRACTED image references carry author attribution. user_name may be
    absent (MCP redeem claims carry only the id) — then it is resolved from
    `users` here, once per task (same join precedent as checkpoints' resolver).
    """
    from files_service import extract_and_replace_images
    from markdown_normalize import normalize_markdown

    from db import fetch_one, get_db
    from jobs.tasks._dead_letter import dead_letter
    from ydoc_store import set_content

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return

    db = await get_db()
    project_id = ref.get("project_id", "")

    async def _mark_error() -> None:
        await db.query(
            "UPDATE type::record('documents', $id) SET processing_status = 'error'",
            {"id": reference_id},
        )
        # WHY every error exit (missing/bad file_path, blank conversion AND
        # converter failure) notifies the UI via WS, not just the exception path:
        # no silent degradation — the early returns must surface in the panel the
        # same way a converter error does, instead of waiting for the next poll.
        await event_bus.emit("reference_status_changed", reference_id=reference_id,
                             project_id=project_id, status="error")

    await db.query(
        "UPDATE type::record('documents', $id) SET processing_status = 'processing'",
        {"id": reference_id},
    )

    rel_path = ref.get("file_path", "")
    if not rel_path:
        await _mark_error()
        return

    abs_path = (STORAGE_PATH / rel_path).resolve()
    if not abs_path.is_relative_to(STORAGE_PATH.resolve()) or not abs_path.is_file():
        logger.error("convert_upload: bad/missing file for ref %s: %s", reference_id, rel_path)
        await _mark_error()
        return

    try:
        data = await _read_docx_bytes(abs_path)
        markdown = await _post_to_converter(data, abs_path.name)
        # see SYSTEM: markdown content normalize — DOCX/PDF→md entry point. Dedent
        # list-nested fences, reflow wrapped bullets, collapse post-marker spacing
        # before image extraction — same choke point as the .md upload path
        # (routes/files.py).
        markdown = normalize_markdown(markdown)
        # Blank conversion = the source had no extractable text (e.g. a scanned
        # PDF with no text layer). Landing it as ready+empty reads as success —
        # silent degradation — so it takes the error exit instead (plan
        # pdf-import-pymupdf4llm Risks). Expected-failure shape: no raise, no
        # dead-letter; the guard owns the exit.
        if not markdown.strip():
            logger.warning(
                "convert_upload: blank markdown for ref %s (no text layer in source?)",
                reference_id,
            )
            await _mark_error()
            return
        # WHY import creates a NEW markdown reference and never merges into an
        # existing document via OT: avoids OT conflicts on import.
        # Extracted images are attributed to the uploader (created_by/created_by_name).
        created_by = user_id or None
        created_by_name = user_name
        if created_by and not created_by_name:
            user_row = await fetch_one("users", user_id)
            created_by_name = (user_row or {}).get("name")
        processed, _image_refs = await extract_and_replace_images(
            markdown, project_id, ref.get("parent_id"),
            created_by=created_by, created_by_name=created_by_name,
        )
    except Exception as e:
        logger.error("convert_upload failed for %s: %s", reference_id, e)
        # Terminal dead-letter: WHEN is dead_letter()'s contract; the recorder is
        # _mark_error (status + reference_status_changed). Mirrors transcribe_task.
        await dead_letter(_mark_error(), e)

    # INVARIANT(corruption): worker has no live collab session — set_content persists AND publishes
    # the Yjs update to the backplane ydoc:{id} channel for the web replica to broadcast.
    # Why: the worker holds no editors; set_content is the only path that writes Surreal
    # AND fans the update to live editors (a plain DB UPDATE would leave them stale).
    await set_content(reference_id, processed, persist=True,
                      extra_sets={"processing_status": "ready"})
    # D1b: set_content does NOT emit content_flushed, so emit here once per worker write
    # (mirrors _finalize_content_mutation on the REST path) so embeddings._on_content_flushed
    # schedules the embed. Without it an imported .docx is persisted + broadcast but never
    # embedded — the 4-of-179-docs shape in the plan (only editor-flushed docs embedded).
    # Same caveat as transcribe_task: a live editor's replica may emit its own flush event
    # for this write; the embed debounce collapses the pair.
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=reference_id, project_id=project_id)
    logger.info("Convert import done: %s", reference_id)
    await event_bus.emit("reference_status_changed", reference_id=reference_id,
                         project_id=project_id, status="ready")


async def convert_docx_task(ctx, reference_id: str, user_id: str,
                            user_name: str | None = None) -> None:
    """Convert an uploaded .docx reference into a Markdown reference.

    Thin arq-visible wrapper over `_convert_upload_task` (name kept verbatim —
    the wire and 10 call/patch sites use it; a rename would strand jobs
    enqueued across a deploy).
    """
    await _convert_upload_task(ctx, reference_id, user_id, user_name)


async def convert_pdf_task(ctx, reference_id: str, user_id: str,
                           user_name: str | None = None) -> None:
    """Convert an uploaded .pdf reference into a Markdown reference.

    Thin arq-visible wrapper over `_convert_upload_task` — the converter
    branches on the stored file's extension, so the body is identical to the
    .docx path (plan pdf-import-pymupdf4llm).
    """
    await _convert_upload_task(ctx, reference_id, user_id, user_name)
