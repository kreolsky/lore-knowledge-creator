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
# bind, /followup, the empty-assistant cleanup) lives in
# `completions_harness`. They are imported +
# re-exported below. This module keeps the HTTP route: validation, the turn
# lock, message rows, context assembly, and the hand-off.
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
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from surrealdb import AsyncSurreal
from turn_lock import (
    _record_turn_lock_rejection,
    acquire_turn_lock,
    release_turn_lock,
)

from auth import get_current_user
from db import get_db
from models import CompletionRequest
from routes.chat._router import router

# The empty-assistant cleanup lives with the turn path that calls it
# (completions_harness); imported for use in setup here and re-exported for
# existing importers/tests. There is no leaf move before a turn: a branch is
# its own session and nothing re-points the dsh log — a non-tail parent is a
# stale client, answered 409 by _require_tail_parent.
from routes.chat.completions_harness import _delete_empty_assistant  # noqa: F401

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

    Built by `_prepare_agent_turn_setup` under the turn lock; consumed by
    `run_harness_turn` (completions_harness)."""

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


async def _create_turn_messages(
    db, session_id: str, body: CompletionRequest, model: str,
    user_msg_id: str, assistant_msg_id: str,
) -> None:
    """Batch-create the turn's user + placeholder-assistant rows (or 500)."""
    last_msg = body.messages[-1]
    user_images = last_msg.images
    batch_result = await db.query(
        "CREATE type::record('messages', $uid) SET chat_id = $cid, parent_id = $pid, role = 'user', content = $uc"
        + (", images = $imgs" if user_images else "")
        + ";"
        "CREATE type::record('messages', $aid) SET chat_id = $cid, parent_id = $uid, role = 'assistant', content = '', model = $model;",
        {
            "uid": user_msg_id,
            "aid": assistant_msg_id,
            "cid": session_id,
            "pid": body.parent_id,
            "uc": last_msg.content,
            **({"imgs": user_images} if user_images else {}),
            "model": model,
        },
        site="chat",
    )
    if isinstance(batch_result, str):
        logger.error("Batch message create SDK error: %s", batch_result)
        raise HTTPException(status_code=500, detail="Failed to create messages")


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
    model: str, user_msg_id: str, assistant_msg_id: str, db,
    line: DriverLine | None = None,
    reasoning_effort: str | None = None,
) -> _AgentTurn:
    """Message rows + context assembly, sequenced (each step's WHY lives on its
    own helper). A refusal here is a REAL error status — the POST answers
    JSON, there is no stream to ride.

    # ARCH: a branch is its own session bound to
    # ONE dsh log (dsh id = Lore id) — there is NO leaf move before the turn.
    # The parent-tail contract is enforced by _require_tail_parent in
    # create_completion (a stale client 409s before any row is written)."""
    await _create_turn_messages(db, session_id, body, model, user_msg_id, assistant_msg_id)

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
    exc: Exception, db, session: dict, session_id: str, user: dict,
    assistant_msg_id: str,
) -> NoReturn:
    """Terminal handler for a setup failure raised before the turn hand-off:
    clean up, then re-raise with a status the caller can read. Never returns —
    the caller's fall-through is dead code, and `NoReturn` is what says so
    without the reader having to prove it.

    The message rows already exist here — `_create_turn_messages` runs before
    `build_context` RPCs the line for the agent capability, so a line outage in
    between lands HERE (never the uncaught handler): the empty assistant row is
    cleaned up and the failure surfaces explained — never a bare 500 plus a
    blank turn on the next read. Cleanup is best-effort — it must not mask the
    real error.
    """
    logger.exception("Agent turn setup failed before the hand-off")
    try:
        await _delete_empty_assistant(db, assistant_msg_id)
    except Exception:
        logger.warning("setup placeholder cleanup failed", exc_info=True)
    try:
        from driver.persistence import _record_turn_error
        await _record_turn_error(
            assistant_msg_id=assistant_msg_id, session_id=session_id,
            reason="agent_setup_failed", source="setup",
            user_id=user.get("user_id", ""), project_id=session.get("project_id"),
        )
    except Exception:
        logger.warning("setup turn_error telemetry failed", exc_info=True)
    # INVARIANT (persisted): a pre-turn failure leaves NO placeholder assistant
    # row, and a NAMED failure carries a named status — the same wording family
    # the in-turn failure frames give, never a bare 500.
    # Why: the row is already written when the capability read fails, so both
    # halves are needed for the thread to read honestly afterwards.
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
    """The pre-lock validation cluster: note/API/rate-limit guards and
    model + reasoning-effort resolution.
    Raises on refusal; returns the resolved (model, reasoning_effort)."""
    await _validate_chat_turn(session, user)
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


async def _acquire_turn_lock_or_503(session_id: str) -> str | None:
    """Acquire the per-session turn lock; a store outage is a NAMED 503.

    WHY 503: the shared bounded client (redis_pool)
    raises within _SOCKET_TIMEOUT_S on a wedged store — a named outage gets a
    named status, mirroring the 409 the caller answers on busy. 409 would lie
    here: no turn is in progress, the STORE is down. Returns None on busy
    (the caller's reject-before-create 409 path)."""
    try:
        return await acquire_turn_lock(session_id)
    except (RedisConnectionError, RedisTimeoutError) as exc:
        raise HTTPException(
            status_code=503, detail="Turn lock store unavailable",
        ) from exc


async def _require_tail_parent(db, session_id: str, parent_id: str | None) -> None:
    """The linear-turn contract: a session's rows
    only ever APPEND, so a turn's parent_id must name the session's live TAIL
    — the row no other live row points at (the newest leaf on a legacy tree,
    i.e. the newest lineage the UI shows).

    - parent null + empty session → the genuine first turn: OK.
    - parent null + rows present → 409: the first message is immutable (a
      branch shares at least the first turn — no root-level forks).
    - parent not a live row of this session → 422 "branch point not
      resolvable" — the chain is corrupt, the same honest refusal the
      branch-point walk gives.
    - parent live but not the tail → 409: another turn landed first; the
      client re-reads the branch and retries once (the plan's contract).

    Why 409 (not 422) for a live non-tail parent: the request is well-formed
    and the branch point resolvable — the client's VIEW is merely stale, and
    the status must say "conflict, re-read", never "unanswerable".
    """
    rows = await db.query(
        "SELECT meta::id(id) AS mid, parent_id, created_at FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": session_id},
        site="chat",
    ) or []
    if not rows:
        if parent_id is None:
            return
        raise HTTPException(
            status_code=422, detail="branch point not resolvable",
        )
    if parent_id is None:
        raise HTTPException(
            status_code=409,
            detail="parent_id is not the tail of this branch — "
                   "re-read the branch and retry",
        )
    parented = {r.get("parent_id") for r in rows}
    leaves = [r for r in rows if r.get("mid") not in parented]
    tail = max(leaves or rows, key=lambda r: r.get("created_at") or "")
    if parent_id != tail.get("mid"):
        if not any(r.get("mid") == parent_id for r in rows):
            raise HTTPException(
                status_code=422, detail="branch point not resolvable",
            )
        raise HTTPException(
            status_code=409,
            detail="parent_id is not the tail of this branch — "
                   "re-read the branch and retry",
        )


async def _require_tail_parent_locked(
    db, session_id: str, parent_id: str | None, lock_token: str,
) -> None:
    """_require_tail_parent under the held turn lock; a refusal releases it.

    WHY: the tail check runs UNDER the lock — checked before it, a turn that
    lands between the check and the acquire would let this one append onto
    a stale parent (a silent fork inside a linear session). A refusal is a
    normal stale-client answer, not a setup failure: release and re-raise.
    """
    try:
        await _require_tail_parent(db, session_id, parent_id)
    except Exception:
        await release_turn_lock(session_id, lock_token)
        raise


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


# INVARIANT: the per-session turn lock is acquired in create_completion HERE, before
# message creation (reject-before-create), not inside the turn hand-off. Why: messages
# created before the lock orphan on contention/abort — two concurrent completions both
# batch-created rows, then one lost the lock deep in the turn path, leaving
# orphan user+assistant rows. The lock is the per-session turn serializer and owns the
# whole message-create + turn lifecycle; on the accepted path it is released by the
# channel's on_end when the driver closes the turn (completions_harness ARCH).
@router.post("/sessions/{session_id}/completions")
async def create_completion(
    session_id: str,
    body: CompletionRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Start an LLM turn. Saves user + assistant messages to DB, hands the
    turn to the driver, answers JSON `{accepted, user_msg_id,
    assistant_msg_id, dsh_session_id}` — the frames ride the project WS.

    # ARCH: the resolved line owns the transcript and is the ONLY AI
    # line (module header); when the line is unconfigured the turn degrades
    # EXPLICITLY — surfaced error naming the line, no fallback. The gate is
    # turn.line (resolve_driver_line → None).
    """
    session = await _require_session_access(session_id, user)
    model, reasoning_effort = await _validate_turn_and_model(session, body, user)

    user_msg_id, assistant_msg_id = str(uuid4()), str(uuid4())

    # INVARIANT (above) — the lock is held from here on.
    lock_token = await _acquire_turn_lock_or_503(session_id)
    if lock_token is None:
        logger.warning("turn lock busy on chat_session %s", session_id)
        await _record_turn_lock_rejection(user, session, session_id, assistant_msg_id)
        raise HTTPException(
            status_code=409,
            detail="A turn is already in progress on this chat.",
        )
    await _require_tail_parent_locked(db, session_id, body.parent_id, lock_token)

    # The agent line is resolved ONCE per request here and threaded to the
    # turn hand-off via _AgentTurn.
    line = await resolve_driver_line()
    # Once the lock is held, ANY setup failure before run_harness_turn takes
    # ownership must release it — else the session wedges until the lock TTL expires.
    try:
        await _lazy_seed_branch_session(db, session, session_id)
        turn = await _prepare_agent_turn_setup(
            session, session_id, body, user, model, user_msg_id, assistant_msg_id, db,
            line=line, reasoning_effort=reasoning_effort,
        )
    except Exception as exc:
        await release_turn_lock(session_id, lock_token)
        await _fail_pre_stream_setup(exc, db, session, session_id, user, assistant_msg_id)

    from routes.chat.completions_harness import run_harness_turn

    return await run_harness_turn(turn, db, lock_token)


# ─── Cancel completion (the driver's ONE cancel arm) ─────────────────────────


@router.post("/sessions/{session_id}/completions/cancel")
async def cancel_completion(
    session_id: str,
    user: dict = Depends(get_current_user),
):
    """Abort the session's in-flight turn.

    # ARCH: the turn is DRIVER-owned — Stop routes into the plugin's ONE
    # cancel arm (POST /stop), on the DRIVER session id (compacted_from for
    # continuation chats). The aborted turn's close through the channel
    # releases the turn lock; there is no backend-side stop signal.
    """
    session = await _require_session_access(session_id, user)
    if session.get("is_note"):
        raise HTTPException(status_code=400, detail="Not a chat session")
    from driver.timeline import post_stop

    try:
        await post_stop(session.get("compacted_from") or session_id)
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
    return {"cancelled": True}
