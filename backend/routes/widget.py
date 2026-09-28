"""Widget routes — desktop voice recorder upload, status and batch extract."""
# ARCH: API-key auth (not JWT). Desktop widget authenticates via per-document
# API keys resolved by get_api_key_context, bypassing session cookies entirely.
# ARCH (plan widget-extract-batch-api): /api/widget/extract is a SEPARATE surface
# from /api/widget/upload on purpose — upload promises the transcription_complete
# → wet-extraction path; extract promises NO document. The two coexist on one key.

import json
import logging
import mimetypes

import settings
from documents.service import find_reference_by_idempotency_key
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from files_util import save_audio_upload
from pipeline.extractor.params import resolve_extractor_params
from pydantic import ValidationError
from transcription import enqueue_transcription

from config import AUDIO_MIMES
from db import extract_id, fetch_one, get_db
from jobs import pool as jobs_pool
from models import WidgetSession, is_ref_row
from routes.api_keys import get_api_key_context

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/api/widget/upload")
async def widget_upload(
    file: UploadFile = File(...),
    ctx: dict = Depends(get_api_key_context),
):
    """Upload audio from the desktop widget and enqueue transcription."""
    project_id = ctx["project_id"]
    document_id = ctx["scope_root"]

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if mime not in AUDIO_MIMES:
        logger.warning("Widget upload rejected — unsupported MIME: %s", mime)
        raise HTTPException(status_code=400, detail=f"Only audio files accepted, got: {mime}")

    original_name = file.filename or f"widget-recording.{mime.split('/')[-1]}"
    # Attributed to the key-owning user (resolve_api_key already resolved the display
    # name for RBAC) — the same human transcription is enqueued for below.
    ref_id, _, _ = await save_audio_upload(
        file, mime, original_name, project_id, document_id,
        title=original_name, processing_status="queued",
        created_by=ctx["user_id"],
        created_by_name=(ctx.get("user") or {}).get("name"),
    )

    if await settings.get("STT_API_URL"):
        await enqueue_transcription(ctx["user_id"], ref_id)

    return {"reference_id": ref_id, "status": "queued"}


@router.get("/api/widget/info")
async def widget_info(ctx: dict = Depends(get_api_key_context)):
    """Return the key's target project and document names for the settings UI."""
    # INVARIANT: serial reads on the shared Surreal conn — never gather (single WS reader).
    # Why: concurrent queries contend ~10-20x slower; two reads stay cheap serial.
    doc = await fetch_one("documents", ctx["scope_root"])
    project = await fetch_one("projects", ctx["project_id"])
    return {
        "project_id": ctx["project_id"],
        "project_name": project.get("name") if project else None,
        "document_id": ctx["scope_root"],
        "document_title": doc.get("title") if doc else None,
    }


@router.get("/api/widget/status/{reference_id}")
async def widget_status(
    reference_id: str,
    ctx: dict = Depends(get_api_key_context),
):
    """Poll transcription status for a widget-uploaded reference."""
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    if ref.get("parent_id") != ctx["scope_root"]:
        raise HTTPException(status_code=403, detail="Reference does not belong to this key's document")

    return {
        "reference_id": reference_id,
        "status": ref.get("processing_status"),
        "content": ref.get("content", "") if ref.get("processing_status") == "ready" else None,
    }


# ─── Batch extract: audio → JSON of config variables, no document ────────────


def _parse_widget_session(raw: str) -> WidgetSession:
    """Parse the `session` form field. 400 (not 422): the caller is an API-key
    utility reading a JSON error body, not a browser form."""
    # 400 not 422: a missing form field lands here too (Form default ""), so a
    # silent field omission is a named client error.
    try:
        data = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail='session must be a JSON object: {"id": "<external session id>"}',
        )
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail='session must be a JSON object: {"id": "<external session id>"}',
        )
    try:
        return WidgetSession.model_validate(data)
    except ValidationError:
        raise HTTPException(
            status_code=400,
            detail="session.id is required and must be a non-empty string",
        )


def _extract_state(ref: dict) -> dict:
    """The reference's file_meta.extract sub-object ({} when absent)."""
    return ((ref.get("file_meta") or {}).get("extract")) or {}


async def _resolve_single_extract_config(reference_id: str, ctx: dict):
    """Exactly ONE agent_configs row on the sandbox document.

    Uses the SAME resolver the editor and MCP use (incl. its require_project_full
    gate on the key-owning user) — 0 rows raises the resolver's own 400. >1 row
    is ambiguous for this surface until T4 adds the protocol switch.
    """
    params_list = await resolve_extractor_params(reference_id, ctx["user"])
    if len(params_list) > 1:
        raise HTTPException(
            status_code=400,
            detail="session.protocol is not implemented yet; "
                   "one agent config per widget document",
        )
    return params_list[0]


async def _enqueue_widget_extract(params) -> None:
    # Default queue — STT deliberately runs outside STT_CONCURRENCY (the WHY is
    # at the worker registration). Stable job_id collapses a still-running
    # duplicate (arq dedups on the pending job key; keep_result=0 frees it at
    # terminal state).
    await jobs_pool.enqueue(
        "widget_extract_task", params.reference_id,
        source_doc_id=params.source_doc_id,
        config_doc_id=params.config_doc_id,
        target_doc_id=params.target_doc_id,
        project_id=params.project_id,
        title_template=params.title_template,
        model=params.model,
        job_id=f"widget-extract:{params.reference_id}",
    )


async def _init_extract_state(reference_id: str, session: WidgetSession) -> None:
    """The ONE whole-object write of file_meta.extract — immediately after
    save_audio_upload, before anything else exists.

    INVARIANT: every LATER write is a nested field path (SET
    file_meta.extract.status = …). Why: a whole-object SET here after the task
    started would drop session/started_at the earlier writes made; the object
    exists from this point on, so nested merges never error.
    """
    # WHY: the object arrives as a $meta param, never as a SurrealQL object
    # literal — `session` is a protected variable name in SurrealQL and a
    # literal key of that name is rejected ("'session' is a protected variable
    # and cannot be set"). Params are data, not parsed, so the stored KEY keeps
    # the plan's name. started_at is time::now() here AND in the failed-replay
    # reset — one type (datetime) for the field.
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET file_meta.extract = $meta, "
        "file_meta.extract.started_at = time::now()",
        {
            "id": reference_id,
            "meta": {
                "status": "queued",
                "session": session.model_dump(exclude_none=True),
            },
        },
    )


async def _replay_hit_response(existing: dict, ctx: dict, session: WidgetSession) -> dict:
    """Serve an idempotency-key hit: the existing job, reset if it failed.

    A failed job is re-enqueued (the stable arq job_id is freed at terminal
    state, keep_result=0), so one STT outage never burns a session id. A hit
    with NO extract state at all is a crash window between row-create and init
    (or a first POST that 400'd on config resolution) — treated as a fresh
    tail: init (with the REQUEST's session — the row never got one) + resolve
    + enqueue.
    """
    ref_id = extract_id(existing["id"])
    status = _extract_state(existing).get("status")

    if status in ("failed", None):
        params = await _resolve_single_extract_config(ref_id, ctx)
        if status == "failed":
            db = await get_db()
            await db.query(
                "UPDATE type::record('documents', $id) SET "
                "file_meta.extract.status = 'queued', "
                "file_meta.extract.error = NONE, "
                "file_meta.extract.started_at = time::now()",
                {"id": ref_id},
            )
        else:
            await _init_extract_state(ref_id, session)
        await _enqueue_widget_extract(params)
        status = "queued"

    return {"job_id": ref_id, "status": status}


async def _create_extract_job(
    file, mime: str, original_name: str, ctx: dict, sess: WidgetSession, idem: str,
) -> dict:
    """The miss path: save the audio, resolve the ONE config, init state, enqueue."""
    try:
        ref_id, _, _ = await save_audio_upload(
            file, mime, original_name, ctx["project_id"], ctx["scope_root"],
            title=original_name, processing_status="queued",
            created_by=ctx["user_id"],
            created_by_name=(ctx.get("user") or {}).get("name"),
            idempotency_key=idem,
        )
    except Exception:
        loser_hit = await find_reference_by_idempotency_key(ctx["project_id"], idem)
        if loser_hit:
            return await _replay_hit_response(loser_hit, ctx, sess)
        raise

    # Config resolution happens HERE, not in the worker — the key-owning user's
    # require_project_full gate runs in the web process; the worker resolves
    # nothing. A 400 here leaves the audio row (an accepted upload); a replay
    # of the same session id re-runs this tail once the config exists.
    params = await _resolve_single_extract_config(ref_id, ctx)

    await _init_extract_state(ref_id, sess)
    await _enqueue_widget_extract(params)
    return {"job_id": ref_id, "status": "queued"}


@router.post("/api/widget/extract")
async def widget_extract(
    file: UploadFile = File(...),
    session: str = Form(""),
    ctx: dict = Depends(get_api_key_context),
):
    """Upload one recording under a widget key; receive a job that turns it into
    the config's variables as JSON — no document is created.

    `session` is a JSON form field: {"id": "<external session id>", "protocol":
    "<reserved, T4>"}. The session id is the idempotency key: a replay returns
    the SAME job (reset + re-enqueued if that job failed).
    """
    sess = _parse_widget_session(session)

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or ""
    if mime not in AUDIO_MIMES:
        logger.warning("Widget extract rejected — unsupported MIME: %s", mime)
        raise HTTPException(status_code=400, detail=f"Only audio files accepted, got: {mime}")

    project_id = ctx["project_id"]
    # WHY project-scoped key: idx_documents_idem is UNIQUE over the BARE field,
    # so a key without the project would let two clinics sharing a session id
    # collide into a 500 (the lookup is project-scoped, the index is not).
    idem = f"widget-extract:{project_id}:{sess.id}"

    # ARCH: SELECT-before-create replay guard (the files.py pattern in full);
    # the unique index backs the simultaneous window, _create_extract_job's
    # catch-arm serves the race loser.
    existing = await find_reference_by_idempotency_key(project_id, idem)
    if existing:
        return await _replay_hit_response(existing, ctx, sess)

    original_name = file.filename or f"widget-recording.{mime.split('/')[-1]}"
    return await _create_extract_job(file, mime, original_name, ctx, sess, idem)


@router.get("/api/widget/extract/{job_id}")
async def widget_extract_status(
    job_id: str,
    ctx: dict = Depends(get_api_key_context),
):
    """Poll a widget-extract job. `variables` is null unless status == done."""
    ref = await fetch_one("documents", job_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Job not found")
    if ref.get("parent_id") != ctx["scope_root"]:
        raise HTTPException(
            status_code=403,
            detail="Reference does not belong to this key's document",
        )

    state = _extract_state(ref)
    status = state.get("status")
    return {
        "job_id": job_id,
        "status": status,
        "variables": state.get("variables") if status == "done" else None,
        "transcript_ref": job_id,
        "error": state.get("error"),
    }
