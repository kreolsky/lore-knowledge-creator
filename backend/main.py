"""Lore — FastAPI backend entry point.

All routes are mounted under /api via APIRouter modules.
SurrealDB 3.x: type::record() instead of type::thing().
SDK 1.0.4: db.query() returns a flat list of records (not list-of-lists).
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager

# Load-bearing import (SYSTEM: instance-settings): importing settings registers
# this process's instance_settings_changed cache-drop, so the backplane
# subscription at the end of the lifespan below picks up the channel. Without
# this import the web process would never drop its override cache on another
# replica's PUT.
import settings  # noqa: E402 — load-bearing bus-subscription import (see above)
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from http_clients import close_all as _close_http_clients
from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX
from pipeline.extractor.runner import on_transcription_complete
from redis_pool import close_redis
from starlette.requests import Request as _StarletteRequest

from config import CHAT_MAX_IMAGE_SIZE_MB
from db import (
    SdkContractError,
    apply_schema,
    mark_startup_complete,
    verify_sdk_contract,
)
from event_bus import on as _bus_on
from migrations import ensure_service_user, seed_admin

# WHY: starlette ≥0.40 reworked formparsers — the old class attribute
# `MultiPartParser.max_file_size` is gone. The per-part reject limit is now
# `max_part_size`, a keyword arg of `Request.form()`/`Request._get_form()`
# (default 1MB), passed through to `MultiPartParser.__init__`. FastAPI calls
# `request.form()` with no args, so we raise the keyword DEFAULT to
# MAX_AUDIO_SIZE_MB. A class-attribute monkeypatch no longer works because
# `__init__` overwrites it from the kwarg on every instance.
# WHY: parser must not reject uploads before the route handler's own size check.
# WHY: patch both `form` and `_get_form` kwdefaults — `form` forwards the
# value to `_get_form` explicitly, so patching only the class attr is a no-op.
# Why: __init__ re-reads this kwarg on every instance, overwriting any class attr.
# DEBT: mutating starlette Request.form/__get_form kwdefaults at import time
#   instead of passing max_part_size per-route — Why deferred: FastAPI calls
#   request.form() with no args and exposes no per-request knob, so a global
#   kwdefault is the only non-monkeypatched injection point; revisit when
#   starlette exposes a clean per-app upload-size config.
# The VALUE leg is the import-time env⊕default (settings.bootstrap_value): a
# live MAX_AUDIO_SIZE_MB override cannot bind here (no loop/DB at import), so it
# lands on the route-level checks, which read settings.get() — the parser bound
# stays a best-effort early reject.
_max_part_bytes = settings.bootstrap_value("MAX_AUDIO_SIZE_MB") * 1024 * 1024
_StarletteRequest.form.__kwdefaults__["max_part_size"] = _max_part_bytes
_StarletteRequest._get_form.__kwdefaults__["max_part_size"] = _max_part_bytes

# collab_events.subscribe_events() registers the REST→collab bridge handlers
# (entity_deleted, documents_deleted_batch, access_changed,
# backlinks_changed_batch, checkpoint_created, document_history_added + the
# note-session trio). The web process calls it explicitly at import — the same
# ownership posture as the embeddings import above. Pinned by
# test_bus_subscriber_reachability.py.
from collab import events as collab_events  # noqa: E402 — next to its twin below
from collab.registry import flush_all_sessions
from logging_conf import setup_logging
from routes import (
    admin_embeddings,
    admin_settings,
    admin_skills,
    api_keys,
    auth,
    cabinet,
    chat,
    checkpoints,
    collab_project_ws,
    document_shares,
    documents,
    driver_settings,
    extractor,
    files,
    files_mcp_upload,
    health,
    invites,
    pipeline_schedules,
    preferences,
    project_ws,
    projects,
    projects_members,  # noqa: F401 — import registers members/invites routes on projects.router
    public_share,
    references,
    sandbox,
    search,
    telemetry,
    tool_api,
    users,
    widget,
)

# ARCH: import `embeddings` at STARTUP so its module-level `_bus_on("content_flushed",
# _on_content_flushed)` (embeddings.py:411) is subscribed from process start. Before this,
# nothing imported `embeddings` at startup — it was imported lazily inside this lifespan's
# SHUTDOWN half and inside `jobs/tasks.py` task bodies — so the content_flushed→embed
# scheduler was unsubscribed until something else happened to import it (in practice: the
# first chat retrieval). Everything flushed before that was silently never embedded, and
# `embedding_status='ok'` proved nothing (it is the schema DEFAULT). The import IS the
# fix: registering the handler from main.py instead was rejected because it splits a
# subscription from the module that owns it, and the next `_bus_on` added in embeddings.py
# would be missed the same way. See test_bus_subscriber_reachability.py.
#
# This line IS the load-bearing one for the WEB process: the jobs package facade
# was emptied (plan fewer-layers), so importing `jobs` no longer pulls
# `jobs.worker` (which imports embeddings eagerly for the worker process). The
# worker still wires its own subscription at boot; the web process wires it HERE.
import embeddings  # noqa: F401 — load-bearing bus-subscription import (see above)

collab_events.subscribe_events()

setup_logging()
logger = logging.getLogger(__name__)


# There is no per-startup sort_key repair sweep: every creation path is closed at
# source — seed_dev routes every document through documents.service.create_document,
# and routes/projects.py::create_project sets the index doc's sort_key in its
# transactional CREATE. The one-time backfill for rows predating the field has run
# on every installation and is retired with the other applied migrations.
# INVARIANT preserved: every non-reference document gets a sort_key at creation time.


async def sweep_orphan_proposal_fields(db=None) -> int:
    """Per-startup safety net: drop orphan `proposal.*`/`proposals.*` field
    definitions from `messages`, AND clear the values they left behind.

    # DEBT: recurring per-boot schema-introspect sweep instead of a trusted
    #   migration marker — Why deferred: a restored backup can carry a NEWER
    #   migrations.applied marker over an OLDER schema that still DECLAREs the
    #   orphan fields; the migration runner trusts the marker and skips, so the
    #   recurring marker-independent introspect is the only self-healing net.
    #   Pay it down once backup-restore guarantees schema↔marker parity.

    # ARCH: a restored backup can carry a NEWER
    `app_meta:migrations.applied` marker (`proposals_to_rows` listed as applied)
    over an OLDER schema that still DECLAREs `proposal.doc_id TYPE string` and
    siblings as separate nested fields. The migration runner trusts the marker
    and skips `proposals_to_rows`, so the orphan non-option children survive —
    and they coerce-fail EVERY message CREATE (`Expected string but found NONE`),
    breaking all chat (Ask + Agent alike) at the first DB write.

    This sweep is marker-independent and self-healing: it introspects the live
    `messages` schema and removes ANY `proposal`/`proposals`-prefixed field
    definition, regardless of which children linger. Idempotent (no-op on a
    clean schema) and runs every boot (a recurring safety check), NOT inside the
    one-time migration runner. `db` is injectable for tests.
    """
    if db is None:
        from db import get_db
        db = await get_db()
    try:
        info = await db.query("INFO FOR TABLE messages")
    except Exception as e:
        logger.warning("sweep_orphan_proposal_fields: schema introspect failed: %s", e)
        return 0
    fields = info.get("fields", {}) if isinstance(info, dict) else {}
    orphans = [
        name for name in fields
        if name == "proposal" or name == "proposals"
        or name.startswith("proposal.") or name.startswith("proposals.")
    ]
    for name in orphans:
        try:
            await db.query(f"REMOVE FIELD IF EXISTS {name} ON messages")
        except Exception as e:
            logger.warning("sweep_orphan_proposal_fields: REMOVE %s failed: %s", name, e)
    if orphans:
        logger.warning(
            "sweep_orphan_proposal_fields: removed %d orphan proposal field definition(s): %s",
            len(orphans), orphans,
        )

    # INVARIANT(data-loss): no `messages` row may retain a `proposal`/`proposals` value
    # once the definition is gone. Why: `messages` is SCHEMAFULL, so a logical dump
    # re-imported into a fresh instance rejects every row carrying an undeclared field —
    # and SurrealDB fails the whole INSERT batch around it, not just the offending row.
    # Rehearsed on prod: 11 orphan rows destroyed all 2668 messages on restore.
    # Runs unconditionally, NOT only when `orphans` is non-empty: the definitions are
    # swept on the first boot while the VALUES survive every boot after that, which
    # is exactly the state prod was found in.
    for name in ("proposal", "proposals"):
        try:
            await db.query(f"UPDATE messages UNSET {name} WHERE {name} != NONE")
        except Exception as e:
            logger.warning("sweep_orphan_proposal_fields: UNSET %s failed: %s", name, e)

    return len(orphans)


async def sweep_orphan_shares(db=None) -> int:
    """Per-startup safety net: hard-delete `document_shares` rows whose `token`
    is NONE.

    These rows are unrecoverable orphans from the pre-plaintext-token model
    (DECISION-PIN in surreal/schema.surql): they carry only a `token_hash`
    whose plaintext was never persisted, so they can never resolve to a working
    link — they would render as `/s/null` plaques. Hard-delete is correct: the
    plaintext is gone, the row is useless. Idempotent (no-op on a clean DB) and
    runs every boot so any environment (dev/test/staging) self-heals without a
    migration marker.
    """
    if db is None:
        from db import get_db
        db = await get_db()
    try:
        await db.query("DELETE FROM document_shares WHERE token IS NONE")
    except Exception as e:
        logger.warning("sweep_orphan_shares: failed: %s", e)
        return 0
    return 0


async def _recover_stuck_transcriptions() -> None:
    """Re-enqueue refs stuck in 'processing' or 'queued' status via arq."""
    try:
        from db import extract_id, get_db
        from jobs import pool as jobs_pool
        from jobs.pool import TRANSCRIPTION_QUEUE
        db = await get_db()
        rows = await db.query(
            "SELECT id, project_id FROM documents "
            "WHERE deleted_at IS NONE AND is_reference = true "
            "AND processing_status IN ['processing', 'queued']"
        )
        if not rows:
            return
        for row in rows:
            ref_id = extract_id(row.get("id"))
            if not ref_id:
                continue
            await db.query(
                "UPDATE type::record('documents', $id) SET processing_status = 'queued'",
                {"id": ref_id},
            )
            await jobs_pool.enqueue("transcribe_task", "__recovery__", ref_id,
                          job_id=f"transcribe:{ref_id}", queue=TRANSCRIPTION_QUEUE)
            logger.info("Re-enqueued stuck transcription: %s", ref_id)
    except Exception as e:
        logger.warning("recover_stuck_transcriptions: %s", e)


# ARCH: Lifespan startup order: schema → migrations → orphan-field sweep → seed → recovery.
# ARCH: flush_all_sessions on shutdown — collab session content is authoritative while alive.
async def _web_stamp_heartbeat() -> None:
    """Publish + periodically refresh the web's code stamp under a TTL.

    Publishes immediately on the first iteration (so a worker that boots concurrently
    reads THIS deploy's stamp, not a leftover from the prior web) then refreshes well
    within the TTL. The TTL (code_stamp.WEB_STAMP_TTL_S) lets a stale/leftover stamp
    from a dead or prior-deploy web expire → the worker's stale_code_guard degrades to
    the absent-grace branch instead of falsely refusing jobs. Best-effort per iteration;
    cancelled on shutdown.
    """
    from backplane import get_backplane
    from code_stamp import (
        CODE_STAMP,
        WEB_STAMP_REDIS_KEY,
        WEB_STAMP_REFRESH_S,
        WEB_STAMP_TTL_S,
    )
    while True:
        try:
            await get_backplane().set_ttl(WEB_STAMP_REDIS_KEY, CODE_STAMP, ttl_s=WEB_STAMP_TTL_S)
            logger.info("web code_stamp published (%s, ttl=%ss)", CODE_STAMP, int(WEB_STAMP_TTL_S))
        except Exception:
            logger.warning("web code_stamp heartbeat failed", exc_info=True)
        await asyncio.sleep(WEB_STAMP_REFRESH_S)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Run DB schema, migrations, and seeding on startup, then yield for app lifetime."""
    # Fail-loud canary: start the web-stamp heartbeat FIRST (before the slow DB sweeps)
    # so a fresh worker booting concurrently reads this deploy's stamp ASAP, not a stale
    # leftover. The TTL + refresh (see _web_stamp_heartbeat) keep it fresh while alive and
    # let it expire when this web dies.
    stamp_task = asyncio.create_task(_web_stamp_heartbeat())
    await apply_schema()
    # WHY: the kind backfill runs in the WEB process, right after apply_schema —
    # the same boot that DEFINEs the column heals the rows predating it. Best
    # effort (a failure warns, never blocks boot): the sweep and each document's
    # next re-embed stamp kind anyway, so a failed backfill only delays the heal.
    from embedding_coverage import backfill_doc_chunk_kind
    try:
        await backfill_doc_chunk_kind()
    except Exception:
        logger.warning("doc_chunks.kind backfill failed; rows stay NONE until re-embed",
                       exc_info=True)
    # WHY: refuse to start if the surrealdb SDK contract has drifted. The monkeypatched
    # _recv_task (fut.done() guard) and the is_record_id / query / query_raw error-shape
    # helpers depend on private SDK internals with no stability guarantee; a silent bump
    # would wedge the reader or break serialization quietly. A broken reader is worse than
    # a startup refusal, so a genuine contract violation (SdkContractError) is fatal here.
    # INVARIANT: catch ONLY SdkContractError — a transient transport error during the probes
    # is a connectivity problem, not SDK drift; it propagates as a normal startup failure  Why: SdkContractError means the DB schema/protocol drifted from what the SDK expects (a real contract break, fatal); a transport error is just the probe failing to connect and must not be misread as drift.
    # (verify_sdk_contract already retries it on a fresh connection) and must NOT be
    # mislabeled/logged as a contract failure. Why: conflating them would refuse startup
    # on a flaky DB connection when the SDK contract is actually fine.
    try:
        await verify_sdk_contract()
    except SdkContractError:
        logger.critical("SurrealDB SDK contract verification failed — refusing to start", exc_info=True)
        raise
    from migrations.runner import run_migrations
    await run_migrations()
    await sweep_orphan_proposal_fields()
    await sweep_orphan_shares()
    await seed_admin()
    await ensure_service_user()
    await _recover_stuck_transcriptions()
    from files_util import sweep_tmp_uploads
    await sweep_tmp_uploads()
    _bus_on("transcription_complete", on_transcription_complete)
    from auth import on_token_version_bumped
    _bus_on("token_version_bumped", on_token_version_bumped)
    mark_startup_complete()
    from event_bus import subscribe_backplane_events
    await subscribe_backplane_events()
    # ARCH: MCP gateway session manager — entered here (not by the mounted sub-app's
    # own lifespan, which Starlette never runs). LIFO close ⇒ the gateway tears
    # down BEFORE flush_all_sessions below on shutdown.
    from mcp_gateway import mcp_lifespan as _mcp_lifespan
    async with _mcp_lifespan():
        yield
    # Stop the web-stamp heartbeat so the TTL expires and a stale stamp can't linger.
    stamp_task.cancel()
    try:
        await stamp_task
    except asyncio.CancelledError:
        pass
    # Graceful shutdown: flush all active collab sessions to DB
    await flush_all_sessions()
    await close_redis()  # the shared string client (SYSTEM: redis-pool)
    # Every pooled httpx client (transcription, embeddings, retrieval, docx,
    # models_catalog, … — SYSTEM: http-clients) closes in ONE place, after the
    # Redis client, before the arq pool (LIFO: clients torn down while the app
    # is otherwise still intact).
    await _close_http_clients()
    from jobs import pool as jobs_pool
    await jobs_pool.close_arq_pool()


app = FastAPI(lifespan=lifespan)

# ARCH: X-Request-ID + uncaught-500 catch-all. `@app.middleware("http")` uses
# add_middleware → insert(0), so registering request_id FIRST in source order
# makes it the INNERMOST user middleware (just outside ExceptionMiddleware).
# WHY innermost is the correct placement: a route/dependency exception is
# re-raised by ExceptionMiddleware and reaches request_id BEFORE passing through
# any other BaseHTTPMiddleware (csrf/json-body/CORS). That matters because anyio
# wraps exceptions propagating out of a BaseHTTPMiddleware task group in a
# BaseExceptionGroup, which ServerErrorMiddleware's `except Exception` does NOT
# catch — so the registered @app.exception_handler(Exception) (a secondary net
# below) would never fire and the raw exception would escape to uvicorn. Catching
# at the innermost user middleware converts it to the response before any task
# group can wrap it. request.state.request_id is still set before the route runs.
# INVARIANT: re-raise asyncio.CancelledError (a BaseException) — never swallow a
# cancellation. HTTPException is handled by the inner ExceptionMiddleware and
# returns normally through call_next, so it is unaffected. Why: swallowing it would
# hang the request task on client disconnect and starve the middleware chain.
REQUEST_ID_HEADER = "X-Request-ID"


def _make_error_response(request: Request, rid: str, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception (request_id=%s %s %s): %s",
        rid, request.method, request.url.path, exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
        headers={REQUEST_ID_HEADER: rid},
    )


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    rid = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    request.state.request_id = rid
    try:
        response = await call_next(request)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _make_error_response(request, rid, exc)
    response.headers[REQUEST_ID_HEADER] = rid
    return response


# WHY: the dev-origin fallback carries EVERY origin a dev/CI browser reaches this app
# from — including the compose-internal http://frontend:5173 the e2e stack uses. The
# csrf_origin_check below 403s any mutation whose Origin is absent here, so a missing
# entry silently kills every browser write on a stack whose .env predates the entry
# (editing .env.example alone reaches only freshly-copied stacks). Kept in step with
# .env.example by .claude/scripts/cors-origins-parity.py (cheap gate + structure-gates).
_DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://localhost:5174,http://frontend:5173"

_cors_raw = os.environ.get("CORS_ORIGINS", "")
if not _cors_raw:
    logging.getLogger(__name__).warning(
        "CORS_ORIGINS not set — defaulting to localhost dev origins"
    )
    _cors_raw = _DEFAULT_CORS_ORIGINS
_CORS_ORIGINS = [o.strip() for o in _cors_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


# ARCH: CSRF defense-in-depth — reject mutating requests with foreign Origin.
# SameSite=Lax cookies block most CSRF, but Origin check covers subdomain attacks
# and older browsers. Placed after CORSMiddleware (Starlette processes bottom→top).
@app.middleware("http")
async def csrf_origin_check(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        path = request.url.path
        # ARCH: /mcp is EXEMPT from the CSRF Origin guard. The MCP gateway
        # authenticates ONLY via the Authorization Bearer header and never reads the
        # lore_session cookie — CSRF is a cookie-credential attack, so there is no
        # vector on /mcp. Keeps MCP clients that send an Origin (some Electron-based
        # clients) working. Safe only because /mcp is token-only: do NOT extend this
        # exemption to any path that reads lore_session.
        if path.startswith("/mcp"):
            return await call_next(request)
        origin = request.headers.get("origin")
        if origin and origin not in _CORS_ORIGINS:
            return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
    return await call_next(request)


_JSON_MAX_BYTES = 1 * 1024 * 1024  # 1MB limit for non-upload JSON endpoints
# ARCH: request-body cap for chat completions (which carry base64 images). Derived
# from the shared CHAT_MAX_IMAGE_SIZE_MB total-attachment budget (config.py). The
# *1.5 factor covers base64 inflation (~33%) + JSON/conversation-history overhead:
# 5MB raw images → ~6.7MB base64, leaving headroom for the text payload. Frontend
# validates the raw sum at attach time so a compliant request always fits.
_LARGE_JSON_MAX_BYTES = int(CHAT_MAX_IMAGE_SIZE_MB * 1.5 * 1024 * 1024)
_UPLOAD_PATHS = (
    "/api/documents/upload", "/api/documents/upload-and-transcribe", "/api/documents/upload-markdown",
    "/api/references/upload", "/api/references/upload-and-transcribe", "/api/references/upload-markdown",
    "/api/widget/upload", "/api/widget/extract", "/api/chat/transcribe",
    # WHY: the MCP signed-URL redeem route is exempt like every other
    # streaming upload. Why: it exists precisely to carry payloads the 1MB JSON cap
    # refuses (that cap on /mcp is what made `content_base64` useless for a 14MB
    # audio chunk) — leaving it capped would 413 the feature with no hint of the
    # cause. Its own caps are the media ones (MAX_AUDIO_SIZE_MB / MAX_IMAGE_SIZE_MB),
    # enforced while streaming in save_audio_upload, not by buffering the body.
    MCP_UPLOAD_ROUTE_PREFIX,
)
# WHY: every path that carries base64 chat attachments shares the SAME cap.
# Why: a chat completion carries the images of the turn it sends; the 1MB default
# 413s any image ≳0.7MB raw.
_LARGE_JSON_PATHS = ("/api/chat/sessions/",)  # payloads with images


@app.middleware("http")
async def json_body_size_limit(request: Request, call_next):
    """Reject oversized JSON payloads on non-upload endpoints."""
    if request.method in ("POST", "PATCH", "PUT"):
        path = request.url.path
        if not any(path.startswith(p) for p in _UPLOAD_PATHS):
            length = request.headers.get("content-length")
            if length:
                # H-6: Malformed content-length header must not crash the server
                try:
                    length_int = int(length)
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length"})
                is_chat = any(path.startswith(p) for p in _LARGE_JSON_PATHS)
                max_bytes = _LARGE_JSON_MAX_BYTES if is_chat else _JSON_MAX_BYTES
                if length_int > max_bytes:
                    # WHY: the chat-completions 413 body carries a numeric
                    # `limit_mb` (the attachment budget) so the client can show a
                    # localized "context too large" message instead of a silent failure. Why: history
                    # images ride along in the body and can exceed the cap even when
                    # per-turn attach validation passed; the frontend keys its error
                    # copy off this shape.
                    content: dict[str, object] = {"detail": "Request body too large"}
                    if is_chat:
                        content["limit_mb"] = CHAT_MAX_IMAGE_SIZE_MB
                    return JSONResponse(status_code=413, content=content)
    return await call_next(request)


# WHY: gzip response compression. Registered LAST in source order so add_middleware's
# insert(0) places it OUTERMOST — it wraps every response, including errors raised by
# the inner request_id/csrf/json-body middlewares. minimum_size=1024 skips tiny
# envelopes (errors, acks). markdown-heavy JSON (references/documents lists) compresses
# 5–10×, shrinking the multi-MB doc-switch payloads over the prod hairpin.
app.add_middleware(GZipMiddleware, minimum_size=1024)


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
# The per-entity collab WS route is deleted (plan fewer-layers): no client ever
# opened it — the frontend speaks only the multiplexed project
# channel below. There is no second /ws/collab/* matcher left to order against.
app.include_router(collab_project_ws.router)
app.include_router(project_ws.router)
app.include_router(users.router)
app.include_router(invites.router)
app.include_router(cabinet.router)
app.include_router(projects.router)
app.include_router(sandbox.router)
app.include_router(search.router)
app.include_router(documents.router)
app.include_router(document_shares.router)
app.include_router(checkpoints.router)
app.include_router(references.router)
app.include_router(public_share.router)
app.include_router(files.router)
app.include_router(files_mcp_upload.router)
app.include_router(preferences.router)
app.include_router(telemetry.router)
app.include_router(api_keys.router)
app.include_router(pipeline_schedules.router)
app.include_router(tool_api.router)
app.include_router(widget.router)
app.include_router(extractor.router)
app.include_router(admin_embeddings.router)
app.include_router(admin_settings.router)
app.include_router(admin_skills.router)
app.include_router(driver_settings.router)

# ARCH: the MCP Universal Agent Gateway is
# mounted at the EXACT path /mcp (and /mcp/) as Route'd raw ASGI apps, NOT a
# Mount — a Mount would 307-redirect bare /mcp → /mcp/, which some MCP HTTP
# clients do not follow. Starlette Route treats the non-function callable
# (McpASGIApp instance) as an ASGI app and forwards it. The middleware chain
# (request_id/csrf/json-body/gzip) wraps these routes. The gateway's session
# manager is entered from this app's lifespan via mcp_gateway.mcp_lifespan().
from mcp_gateway.server import McpASGIApp as _McpASGIApp
from starlette.routing import Route as _StarletteRoute

_mcp_asgi = _McpASGIApp()
app.router.routes.append(_StarletteRoute("/mcp", endpoint=_mcp_asgi, methods=["GET", "POST", "DELETE", "HEAD"]))
app.router.routes.append(_StarletteRoute("/mcp/", endpoint=_mcp_asgi, methods=["GET", "POST", "DELETE", "HEAD"]))


# ARCH: catch-all for UNCAUGHT exceptions only. FastAPI's ExceptionMiddleware
# (inner) still handles HTTPException / RequestValidationError and returns their
# {"detail": ...} envelopes unchanged. This handler runs in Starlette's
# ServerErrorMiddleware (outermost) and is a SECONDARY net — the primary catch is
# request_id_middleware above (see WHY there: a BaseExceptionGroup from user
# BaseHTTPMiddleware middlewares escapes ServerErrorMiddleware's `except Exception`).
# WHY: preserves the {"detail": ...} contract client.ts:65 depends on (it reads
# res.text() as `detail` and does NOT parse a JSON envelope), so we MUST NOT
# introduce a new error shape here.
@app.exception_handler(Exception)
async def uncaught_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    return _make_error_response(request, rid, exc)
