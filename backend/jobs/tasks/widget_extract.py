"""Widget batch-extract task — STT + dry extractor on one arq job, no document.

# The widget extract surface's worker entry. see SYSTEM: jobs, SYSTEM: extractor,
# SYSTEM: transcription.
# INVARIANT: the worker has no live collab session — the transcript write goes
# through ydoc_store.set_content, like every other worker write.
# Why: set_content persists AND publishes to the backplane ydoc:{id} channel;
# a plain DB UPDATE would leave every open editor showing stale text (the same
# contract as jobs/tasks/media.py).
"""
from __future__ import annotations

import logging
from pathlib import Path

import event_bus
from config import STORAGE_PATH
from models import is_ref_row

logger = logging.getLogger(__name__)


def _resolve_stored_audio(ref: dict) -> Path | None:
    """The reference's stored audio as an absolute path INSIDE STORAGE_PATH.

    Path validation verbatim from transcribe_task — a ref row's file_path is
    stored data, never trusted outside STORAGE_PATH. None = unusable.
    """
    rel_path = ref.get("file_path", "")
    if not rel_path:
        return None
    abs_path = (STORAGE_PATH / rel_path).resolve()
    if not abs_path.is_relative_to(STORAGE_PATH.resolve()) or not abs_path.is_file():
        return None
    return abs_path


def _make_failure_recorder(db, reference_id: str, pid: str, landed: dict):
    """Build the ONE failure recorder closure for both phases of the task.

    Whether the transcript already landed (`landed["ok"]`) decides its
    processing_status half.
    Why: the transcript's status vocabulary belongs to the transcription half —
    burning it to 'error' when ONLY the extraction failed would tell the
    reference panel the (fine) transcript is broken; the extract half's failure
    lives in file_meta.extract.status/error, which is what the widget utility
    polls.
    """

    async def _record_failure(err: BaseException) -> None:
        sql = (
            "UPDATE type::record('documents', $id) SET "
            "file_meta.extract.status = 'failed', "
            "file_meta.extract.error = $err"
        )
        if not landed.get("ok"):
            sql += ", processing_status = 'error'"
        await db.query(sql, {"id": reference_id, "err": str(err)})
        # INVARIANT (no silent degradation): the panel must see the failure.
        # Why: transcription_error is the reference panel's failure event (the
        # same shape as transcribe_task's _fail_early) — a DB-only status flip
        # would leave the panel waiting on a job that already failed.
        await event_bus.emit("transcription_error", reference_id=reference_id, project_id=pid)

    return _record_failure


async def _transcribe_stored(abs_path: Path, reference_id: str, record_failure) -> str:
    """Call STT; a failure is terminal (dead-letter + re-raise)."""
    from transcription import transcribe_audio

    from jobs.tasks._dead_letter import dead_letter

    try:
        return await transcribe_audio(abs_path)
    except Exception as e:
        logger.error("widget_extract STT failed for %s: %s", reference_id, e)
        await dead_letter(record_failure(e), e)


async def _stt_phase(db, reference_id: str, ref: dict, record_failure) -> str | None:
    """Transcribe the stored audio and land the transcript; None = stop here.

    Status writes mirror transcribe_task so the reference panel shows exactly
    the states a widget_upload shows.
    """
    from db import fetch_one
    from ydoc_store import set_content

    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "file_meta.extract.status = 'transcribing', processing_status = 'processing'",
        {"id": reference_id},
    )

    abs_path = _resolve_stored_audio(ref)
    if abs_path is None:
        logger.error(
            "widget_extract: bad/missing file for ref %s: %s",
            reference_id, ref.get("file_path", ""),
        )
        # Expected-failure shape (no raise, no dead-letter): the guard owns the
        # exit — same as transcribe_task's early returns.
        await record_failure(RuntimeError(
            f"Stored audio file missing or unreadable: {ref.get('file_path', '')!r}"
        ))
        return None

    transcription = await _transcribe_stored(abs_path, reference_id, record_failure)

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return None

    # INVARIANT(corruption): worker write via set_content — persists AND
    # publishes to editors (module docstring).
    # Why: a plain DB UPDATE here would leave open editors stale; the
    # content_flushed emit below also schedules the transcript's embed.
    await set_content(reference_id, transcription, persist=True,
                      extra_sets={"processing_status": "ready"})
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=reference_id,
                         project_id=ref.get("project_id", ""))
    # WHY: this task NEVER emits transcription_complete — that event is the wet
    # hook's trigger (on_transcription_complete enqueues extract_task, which
    # CREATES a child document). Emitting it here would run the wet extraction
    # too, producing the exact document this surface promises not to make.
    return transcription


async def _dry_extract_phase(db, reference_id: str, *, source_doc_id: str,
                             config_doc_id: str, target_doc_id: str, pid: str,
                             title_template: str | None, model: str | None,
                             record_failure) -> None:
    """Run the DRY extractor (no document, no emit from inside the flow) and
    store the post-Compute variables dict."""
    from pipeline.extractor.params import ExtractorParams
    from pipeline.extractor.runner import run_extractor_dry

    from jobs.tasks._dead_letter import dead_letter

    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "file_meta.extract.status = 'extracting'",
        {"id": reference_id},
    )

    try:
        result = await run_extractor_dry(ExtractorParams(
            reference_id=reference_id,
            source_doc_id=source_doc_id,
            config_doc_id=config_doc_id,
            target_doc_id=target_doc_id,
            project_id=pid,
            title_template=title_template,
            model=model,
        ))
    except Exception as e:
        logger.error("widget_extract extraction failed for %s: %s", reference_id, e)
        await dead_letter(record_failure(e), e)

    # variables = extracted_data after Compute, before Render — the dict
    # normalize_extracted already fills with default: for every config variable.
    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "file_meta.extract.status = 'done', "
        "file_meta.extract.variables = $vars, "
        "file_meta.extract.finished_at = time::now()",
        {"id": reference_id, "vars": result["extracted_data"]},
    )
    logger.info(
        "widget_extract done for ref %s (%d variables)",
        reference_id, len(result["extracted_data"]),
    )


# WHY: this task runs STT on the DEFAULT queue, deliberately outside
# STT_CONCURRENCY. Why: the alternative — a transcribe_task →
# transcription_complete → extraction chain — hangs forever on a dropped
# cross-process event and fires the wet hook (a document appears); and a task on
# the transcription queue would hold an STT slot for the ~60 s of LLM. One
# recording per appointment is the load; revisit when T5 batches. The LLM half
# also runs WITHOUT the _extractor_concurrency semaphore (that guard is the
# web-process MCP path's); arq's max_jobs on the default worker is the only
# bound — what T5's runner inherits and must decide on.
async def widget_extract_task(
    ctx, reference_id: str, *,
    source_doc_id: str, config_doc_id: str, target_doc_id: str,
    project_id: str, title_template: str | None = None,
    model: str | None = None,
) -> None:
    """Transcribe a widget recording and extract config variables — DRY.

    STT (transcribe_audio) + the LLM (run_extractor_dry) happen inside this ONE
    task, so no cross-process event chain exists to hang and the wet hook never
    fires. Params are resolved by POST /api/widget/extract
    (resolve_extractor_params) and frozen into the job kwargs — the worker
    resolves nothing. Every status write is a NESTED field path
    (file_meta.extract.*) and idempotent, so a worker-kill retry simply re-runs
    from the top (see the max_tries WHY above the function).
    """
    from db import fetch_one, get_db

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return

    db = await get_db()
    pid = ref.get("project_id", "")
    landed: dict = {}
    record_failure = _make_failure_recorder(db, reference_id, pid, landed)

    transcription = await _stt_phase(db, reference_id, ref, record_failure)
    if transcription is None:
        return
    landed["ok"] = True

    await _dry_extract_phase(
        db, reference_id,
        source_doc_id=source_doc_id, config_doc_id=config_doc_id,
        target_doc_id=target_doc_id, pid=pid,
        title_template=title_template, model=model,
        record_failure=record_failure,
    )
