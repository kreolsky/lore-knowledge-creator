"""Chat completions — the turn route + cancel (the model-list proxy lives in models_catalog).

# SYSTEM: chat-completions — LLM completion turn (driver-owned, JSON), model proxy.
# ARCH: the agent line is the ONLY AI line — there is no history replay.
# Every chat turn is an agent turn handed to the line's driver over
# the standing channel (run_harness_turn; the gate is the resolved
# turn.line). When the line is unconfigured, the agent line is
# EXPLICITLY unavailable — a surfaced error, never a silent no-op, with NO
# fallback.
#
# The Agent-turn PREP layer (`prepare_agent_turn` / `AgentTurnPlan` + the
# prompt-section builders) lives in `completions_turn`; the model-gateway catalog
# (/models proxy, TTL cache, vision + context-window resolution) lives in
# `models_catalog`; the driver-owned turn itself (fan-out, standing key,
# /followup) lives in `completions_harness`. They are imported + re-exported
# below. This module keeps the HTTP routes: validation, context assembly, the
# hand-off, cancel and the queue discard.
#
# ARCH: the queue is dsh's inbox — a send is ALWAYS handed to the driver,
# which queues it behind a running turn. There is no admission lock and no
# tail check, and no rows are written here: the user + assistant pair is
# minted when dsh claims the message (driver.channel `lore/claim`), from the
# facts the send's prep carries.
#
# ARCH: the driver owns every turn; the POST /completions answer is JSON
# `{accepted, ...}` and the frames ride the project WS (SYSTEM: chat-fanout)
# + the reload path — there is no per-turn stream on this route.
"""

import logging
from dataclasses import dataclass
from typing import NoReturn
from uuid import uuid4

import driver.timeline as timeline
import settings
from agent.tools import MUTATING_TOOLS, agent_toolset
from driver.client import (
    DriverLine,
    DriverLineUnreachable,
    DriverSecretMismatch,
    resolve_driver_line,
)
from fastapi import Depends, HTTPException
from model_access import require_model_access
from rate_limit import check_completion_rate_limit
from surrealdb import AsyncSurreal
from tiers import require_tier

from auth import get_current_user
from db import get_db
from models import CompletionRequest, InboxDiscardRequest
from routes.chat._router import router

# Turn-prep moved to completions_turn;
# imported for use in the turn hand-off + re-exported for existing importers/tests.
from routes.chat.completions_turn import (  # noqa: F401
    AgentTurnPlan,
    prepare_agent_turn,
)
from routes.chat.context import build_context
from routes.chat.sessions import _require_session_access

logger = logging.getLogger(__name__)


@dataclass
class _AgentTurn:
    """One prepared chat turn — everything the turn path needs from setup.

    Built by `_prepare_agent_turn_setup`; consumed by `run_harness_turn`
    (completions_harness)."""

    session_id: str
    session: dict
    body: CompletionRequest
    user: dict
    model: str
    project_id: str | None
    toolset: list
    capability_is_mutating: bool
    ctx: object
    user_msg_id: str
    assistant_msg_id: str
    # ARCH: the session's pinned reasoning
    # effort, resolved beside the model in _validate_turn_and_model. None ⇒
    # Default (harness-resolved) — threaded but absent from the wire payload.
    # Defaulted like `line` so unit-test constructions stay valid; the single
    # production construction (_prepare_agent_turn_setup) passes it explicitly.
    reasoning_effort: str | None = None
    # The resolved agent-line driver descriptor — resolved ONCE per request in
    # create_completion and threaded to every consumer (the turn hand-off).
    # None ⇒ no line configured (the harness path surfaces it explicitly
    # with a 503).
    line: DriverLine | None = None


async def _validate_chat_turn(session: dict, user: dict) -> None:
    """Reject sessions that must not take an LLM turn, in route order:
    note session → rate limit. No gateway gate here: the turn is executed by
    the driver, whose own line gate (resolve_driver_line → None) answers
    availability."""
    if session.get("is_note") is True:
        raise HTTPException(
            status_code=400,
            detail="Note sessions are LLM-disabled; post a message via /messages instead",
        )
    if not await check_completion_rate_limit(user["user_id"]):
        raise HTTPException(
            status_code=429, detail="Rate limit exceeded — too many completions"
        )


def _require_own_attachments(body: CompletionRequest, user: dict) -> None:
    """422 when a file on the sent message is not the caller's own upload
    (see is_own_chat_attachment)."""
    from routes.tool_api.sandbox.lifecycle import is_own_chat_attachment

    for f in body.messages[-1].files or []:
        if not is_own_chat_attachment(user["user_id"], f.id, f.name, f.path):
            raise HTTPException(
                status_code=422, detail=f"Attachment {f.name!r} is not in your sandbox workspace",
            )


def _warn_image_drop(ctx, body: CompletionRequest) -> None:
    """A non-vision model cannot take the user's attached images (a text-only
    model rejects any image_url part and fails the whole turn with an opaque
    400). Surface the drop as a context_warning (no-silent-degradation); the
    gate read lives in build_context — ctx.vision_ok is the ONE driver
    capability reply per turn, and the image parts themselves are stripped in
    prepare_agent_turn off the same flag."""
    user_images = body.messages[-1].images
    if user_images and not ctx.vision_ok:
        ctx.warnings.append({
            "code": "image_not_delivered", "id": None,
            "detail": f"active model does not support image input; "
                      f"{len(user_images)} attachment(s) dropped",
        })


async def _prepare_agent_turn_setup(
    session: dict, session_id: str, body: CompletionRequest, user: dict,
    model: str, user_msg_id: str, assistant_msg_id: str,
    line: DriverLine | None = None,
    reasoning_effort: str | None = None,
) -> _AgentTurn:
    """Context assembly for one send (each step's WHY lives on its own
    helper). A refusal here is a REAL error status — the POST answers JSON,
    there is no stream to ride.

    # ARCH: a branch is its own session bound to
    # ONE dsh log (dsh id = Lore id) — there is NO leaf move before the turn."""
    # ARCH: every AI chat is an agent chat (one line); the tool surface is
    # always the full AGENT_TOOLS set (agent_toolset).
    project_id = session.get("project_id")
    toolset = await agent_toolset()
    capability_is_mutating = bool(
        {t["function"]["name"] for t in toolset} & MUTATING_TOOLS
    )

    ctx = await build_context(body, project_id, session)
    _warn_image_drop(ctx, body)

    logger.info(
        "Completion: model=%s, session=%s, system_prompt_id=%s, session_document_id=%s, capability_mutating=%s",
        model,
        session_id,
        session.get("system_prompt_id"),
        session.get("document_id"),
        capability_is_mutating,
    )
    return _AgentTurn(
        session_id=session_id, session=session, body=body, user=user, model=model,
        reasoning_effort=reasoning_effort,
        project_id=project_id, toolset=toolset,
        capability_is_mutating=capability_is_mutating, ctx=ctx,
        user_msg_id=user_msg_id, assistant_msg_id=assistant_msg_id,
        line=line,
    )


async def _fail_pre_stream_setup(
    exc: Exception, session: dict, session_id: str, user: dict,
    assistant_msg_id: str,
) -> NoReturn:
    """Terminal handler for a setup failure raised before the turn hand-off:
    record it, then re-raise with a status the caller can read. Never returns —
    the caller's fall-through is dead code, and `NoReturn` is what says so
    without the reader having to prove it.

    `build_context` RPCs the line for the agent capability, so a line outage
    lands HERE (never the uncaught handler) and surfaces explained — never a
    bare 500. No row exists yet (the pair is minted at claim), so nothing is
    cleaned up. Telemetry is best-effort — it must not mask the real error.
    """
    logger.exception("Agent turn setup failed before the hand-off")
    try:
        from driver.persistence import _record_turn_error
        await _record_turn_error(
            assistant_msg_id=assistant_msg_id, session_id=session_id,
            reason="agent_setup_failed", source="setup",
            user_id=user.get("user_id", ""), project_id=session.get("project_id"),
        )
    except Exception:
        logger.warning("setup turn_error telemetry failed", exc_info=True)
    # A NAMED failure carries a named status — the same wording family the
    # in-turn failure frames give, never a bare 500.
    if isinstance(exc, DriverLineUnreachable):
        raise HTTPException(
            status_code=502,
            detail=(
                f"Agent line unavailable — the {exc.line_name} service is not "
                "reachable. Enable the agent service to chat."
            ),
        ) from exc
    raise exc


async def _validate_turn_and_model(
    session: dict, body: CompletionRequest, user: dict,
) -> tuple[str, str | None]:
    """The validation cluster: note/API/rate-limit guards, the account tier,
    and model + reasoning-effort resolution.
    Raises on refusal; returns the resolved (model, reasoning_effort)."""
    await _validate_chat_turn(session, user)
    _require_own_attachments(body, user)
    # see SYSTEM: capability-tiers — the 'ai' gate sits HERE, after the note 400
    # and the rate-limit 429, beside require_model_access, so ONE call covers
    # every turn entry (a note-chat's base user never reaches it; a base
    # account's AI-chat turn is refused before the model resolves).
    await require_tier(user, "ai")
    model = body.model or session.get("model") or await settings.get("CHAT_MODEL")
    if not model:
        raise HTTPException(
            status_code=400,
            detail="Set the chat model in Admin panel → Models & APIs",
        )
    # INVARIANT(security): the ONE model-access gate, AFTER resolution — explicit,
    # session-pinned, inherited and revoked-after-pinning models all 403 here.
    # Why: session create/PATCH stay unchecked; the turn is where a model runs.
    await require_model_access(user, model)
    # ARCH: the SESSION is the single source
    # for the effort (no per-request override — the composer PATCHes first).
    # No fallback: None = Default, the harness materializes the adapter
    # default. Validated at PATCH time; a stale row value dies at the router
    # with an explicit 400, never silently.
    reasoning_effort = session.get("reasoning_effort") or None
    return model, reasoning_effort


async def _lazy_seed_branch_session(db, session: dict, session_id: str) -> None:
    """Seed a branch's own dsh log before its first turn. A session carved out of a legacy tree (the
    migration) or forked via /branches carries `seed_source_session` +
    `seed_source_seq` — the (dsh session, seq) whose log holds the branch's
    prefix. The FIRST completion swaps the stamp for the log itself: POST
    /session-fork seeds a fresh dsh session under THIS chat's id (dsh id =
    Lore id), the stamp is cleared, and the turn appends linearly ever after.

    - Carrying only `seed_source_session` (no seq) is the migration's 422
    marker: a lineage with model history no row could name a boundary for
    (pre-harness / pre-pair stamps). The honest answer is today's 422, never
    a silently empty model history.
    - The clear runs only AFTER a confirmed seed or already-exists: a crash
    in between leaves the stamp, and the retry re-POSTs into the plugin's
    idempotency guard instead of forking a second log under the same id.
    - The stamp survives a refusal — the branch stays honestly uncontinuable,
    never half-seeded.
    """
    seed_session = session.get("seed_source_session")
    if seed_session is None:
        return
    seed_seq = session.get("seed_source_seq")
    if not isinstance(seed_seq, int) or isinstance(seed_seq, bool):
        raise HTTPException(status_code=422, detail="branch point not resolvable")
    try:
        await timeline.post_session_fork(
            source_dsh_id=seed_session, seq=seed_seq, new_lore_id=session_id)
    except timeline.DriverForkUnresolvable:
        raise HTTPException(status_code=422, detail="branch point not resolvable")
    except DriverSecretMismatch as exc:
        raise HTTPException(
            status_code=502, detail=DriverSecretMismatch.CAUSE) from exc
    await db.query(
        "UPDATE type::record('chat_sessions', $id) "
        "SET seed_source_session = NONE, seed_source_seq = NONE",
        {"id": session_id},
        site="chat",
    )


async def _require_fresh_user_msg_id(db, user_msg_id: str) -> None:
    """409 when the browser-minted user row id already names a row — a
    replayed or duplicated send would otherwise mint a second turn whose
    claim collides with the existing row."""
    rows = await db.query(
        "SELECT meta::id(id) AS mid FROM type::record('messages', $id)",
        {"id": user_msg_id},
        site="chat",
    ) or []
    if rows:
        raise HTTPException(
            status_code=409, detail="This message was already sent",
        )


@router.post("/sessions/{session_id}/completions")
async def create_completion(
    session_id: str,
    body: CompletionRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Queue an LLM turn: validate, assemble the context, hand the send to the
    driver (dsh queues it behind a running turn). Answers JSON `{accepted,
    user_msg_id, assistant_msg_id, dsh_session_id, message_id}` — `message_id`
    is the dsh inbox message; the rows and frames arrive at its claim, on the
    project WS.

    # ARCH: the resolved line owns the transcript and is the ONLY AI
    # line (module header); when the line is unconfigured the turn degrades
    # EXPLICITLY — surfaced error naming the line, no fallback. The gate is
    # turn.line (resolve_driver_line → None).
    """
    session = await _require_session_access(session_id, user)
    model, reasoning_effort = await _validate_turn_and_model(session, body, user)

    user_msg_id, assistant_msg_id = str(body.user_msg_id), str(uuid4())
    await _require_fresh_user_msg_id(db, user_msg_id)

    # The agent line is resolved ONCE per request here and threaded to the
    # turn hand-off via _AgentTurn.
    line = await resolve_driver_line()
    try:
        await _lazy_seed_branch_session(db, session, session_id)
        turn = await _prepare_agent_turn_setup(
            session, session_id, body, user, model, user_msg_id, assistant_msg_id,
            line=line, reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        await _fail_pre_stream_setup(exc, session, session_id, user, assistant_msg_id)

    from routes.chat.completions_harness import run_harness_turn

    return await run_harness_turn(turn)


# ─── Cancel completion (the driver's ONE cancel arm) ─────────────────────────


@router.post("/sessions/{session_id}/completions/cancel")
async def cancel_completion(
    session_id: str,
    user: dict = Depends(get_current_user),
):
    """Abort the session's in-flight turn.

    # ARCH: the turn is DRIVER-owned — Stop routes into the plugin's ONE
    # cancel arm (POST /stop), on the DRIVER session id (compacted_from for
    # continuation chats). The person's Stop takes the queue back: the
    # inbox is emptied and its messages answer as `returned` ({text, images, files}
    # in send order) for the composer. There is no backend-side stop signal.
    """
    session = await _require_session_access(session_id, user)
    if session.get("is_note"):
        raise HTTPException(status_code=400, detail="Not a chat session")
    from driver.timeline import post_stop

    try:
        reply = await post_stop(
            session.get("compacted_from") or session_id, return_queue=True)
    except DriverSecretMismatch as exc:
        raise HTTPException(
            status_code=502,
            detail=DriverSecretMismatch.CAUSE,
        ) from exc
    except DriverLineUnreachable as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Agent line unavailable — the {exc.line_name} service "
                "is not reachable."
            ),
        ) from exc
    return {"cancelled": True, "returned": reply.get("returned") or []}


@router.post("/sessions/{session_id}/inbox/discard")
async def discard_queued(
    session_id: str,
    body: InboxDiscardRequest,
    user: dict = Depends(get_current_user),
):
    """✕ on a queued send: remove the dsh inbox message before its claim.
    `{removed: false}` (200) when it was claimed in the meantime — its turn is
    the user's feedback, not an error. Owner-only like every AI-chat route;
    note chats are refused."""
    session = await _require_session_access(session_id, user)
    if session.get("is_note"):
        raise HTTPException(status_code=400, detail="Not a chat session")
    try:
        removed = await timeline.post_inbox_discard(
            session.get("compacted_from") or session_id, body.message_id)
    except DriverSecretMismatch as exc:
        raise HTTPException(
            status_code=502, detail=DriverSecretMismatch.CAUSE) from exc
    except DriverLineUnreachable as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Agent line unavailable — the {exc.line_name} service "
                "is not reachable."
            ),
        ) from exc
    return {"removed": removed}
