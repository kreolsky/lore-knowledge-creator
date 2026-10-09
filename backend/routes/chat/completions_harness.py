"""The driver-owned turn path.

Part of the chat-completions system — the SYSTEM marker stays in
completions.py (the mechanical-split precedent: completions_turn). This
module owns ONE transport arm.

The driver owns every turn. This runs AFTER
the shared setup (validation, turn lock, message rows, context —
`_prepare_agent_turn_setup` in completions.py, untouched) and hands the turn
over: ensure_fanout (the channel + the project-WS pump), the per-session
STANDING agent key (agent.keys.get_or_create_session_agent_key), bind_turn
(with the on_end that releases the turn lock when the channel closes the
turn), the preamble frames pushed through the SAME listener queue, and
POST /followup with the documented 409-retry. The HTTP answer is JSON — the
frames live on the project WS (routes.chat.fanout) and the reload path.

# ARCH: the turn lock is held for the WHOLE driver-owned turn — released by
# the channel's on_end callback (graceful close, deadline breach,
# unsubscribe), not by this request's return. Why: reject-before-create
# a second message while the turn runs is
# refused before its rows exist; the driver's own 409 serialize only guards
# the driver-side slot (a lock released at HTTP-return would let rows be
# created for a followup the driver then refuses).

# ARCH: the preamble frames (ids/sources/context_warning) and the failure
# frames (error/turn_closed) ride the SAME listener queue as the driver's
# frames — the preamble BEFORE the followup POST (the queue is the ONE
# ordering point; a driver frame can never overtake the preamble it belongs
# after), the failure frames on a refused turn (no silent degradation: a
# browser that saw `ids` must see the turn die explicitly, and turn_closed
# is the browser's ONLY terminal). These are frame DICTS —
# the WS frame vocabulary the relay arms speak. One exception: a
# line-unavailable/unopenable-channel failure emits NO frames at all (no
# transport exists to carry them) — the HTTP error status is the entire
# signal there.

# ARCH: the turn's session_id on the wire is the DRIVER session id
# (compacted_from or the chat id) — the compaction continuation contract
# shared by the turn path's both arms.
"""
import asyncio
import logging
from typing import TYPE_CHECKING

import driver.timeline as timeline
import settings
from driver.channel import get_driver_channel
from driver.client import (
    PERPLEXITY_SEARCH_PINS,
    WEB_SEARCH_PROVIDERS,
    DriverLineUnreachable,
    DriverSecretMismatch,
    _build_turn_payload,
)
from driver.timeline import DriverTurnBusy
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from turn_lock import _heartbeat_turn_lock, _teardown_turn_lock

from routes.chat.completions_turn import prepare_agent_turn
from routes.chat.fanout import ensure_fanout

if TYPE_CHECKING:  # no runtime import — completions imports THIS module lazily
    from routes.chat.completions import _AgentTurn

logger = logging.getLogger(__name__)


async def _delete_empty_assistant(db, msg_id: str) -> None:
    """Delete a placeholder assistant row that never received content.

    Called on the terminal error paths (line-unavailable / setup-error) where the
    completion ends before the turn hand-off (no frames have ridden the project
    WS yet). Guarded by content = ''
    so a row that did persist partial content is never removed. The USER message is
    always kept — it is valid input the user may retry.
    """
    await db.query(
        "DELETE type::record('messages', $id) WHERE content = '' OR content IS NONE",
        {"id": msg_id},
    )


def _preamble_frames(turn) -> list[dict]:
    """The turn preamble as frame dicts — the shapes the browser's frame
    reducer consumes."""
    frames: list[dict] = [{
        "type": "ids",
        "user_message_id": turn.user_msg_id,
        "assistant_message_id": turn.assistant_msg_id,
    }]
    if turn.ctx.sources:
        frames.append({"type": "sources", "sources": turn.ctx.sources})
    for w in turn.ctx.warnings:
        frames.append({"type": "context_warning", **w})
    return frames


def _emit_failure_frames_quietly(driver_session_id: str, message: str) -> None:
    """error + turn_closed into the listener queue — the setup-failure frame
    shape. turn_closed (not done) because the browser ends a turn ONLY on
    turn_closed; the empty-content done it replaces carried nothing the
    store's done handler reads. Quiet because the channel may be exactly what
    is broken (the frames are best-effort; the HTTP error status is the
    guaranteed signal)."""
    try:
        get_driver_channel().emit_frames(driver_session_id, [
            {"type": "error", "message": message},
            {"type": "turn_closed"},
        ])
    except Exception:
        logger.warning(
            "harness turn: failure frames not delivered session=%s",
            driver_session_id, exc_info=True,
        )


async def _record_error_quietly(turn, reason: str, source: str) -> None:
    try:
        from driver.persistence import _record_turn_error

        await _record_turn_error(
            assistant_msg_id=turn.assistant_msg_id,
            session_id=turn.session_id,
            reason=reason, source=source,
            user_id=turn.user.get("user_id", ""), project_id=turn.project_id,
        )
    except Exception:
        logger.warning("harness turn-error telemetry failed", exc_info=True)


async def _fail_harness_turn(
    turn, db, lock_token: str, heartbeat: asyncio.Task | None,
    driver_session_id: str, *, message: str, status: int,
    reason: str, source: str,
) -> None:
    """The terminal failure ladder: failure frames to the owner, lock
    teardown (the bind's on_end never fires for a turn that never opened —
    release here; a stale unclaimed bind's later overwrite-fire is a fencing
    no-op), placeholder cleanup, telemetry, and the named HTTP refusal."""
    _emit_failure_frames_quietly(driver_session_id, message)
    await _teardown_turn_lock(turn.session_id, lock_token, heartbeat)
    try:
        await _delete_empty_assistant(db, turn.assistant_msg_id)
    except Exception:
        logger.warning(
            "harness turn: placeholder cleanup failed msg=%s",
            turn.assistant_msg_id, exc_info=True,
        )
    await _record_error_quietly(turn, reason, source)
    raise HTTPException(status_code=status, detail=message)


async def _ensure_fanout_or_refuse(
    turn, session_id: str, driver_session_id: str,
) -> None:
    """Open the channel + project-WS pump for this session, refusing in the
    line-unavailable family when the channel cannot open."""
    try:
        fanout_ok = await ensure_fanout(
            session_id, driver_session_id=driver_session_id)
    except Exception as exc:
        # The channel not opening IS the line being unreachable — same
        # refusal family as the line-unavailable arm's DriverLineUnreachable.
        raise DriverLineUnreachable(
            turn.line.name, f"event channel: {exc}") from exc
    if not fanout_ok:
        raise RuntimeError(
            f"fan-out refused for session {session_id} (row flipped "
            f"mid-request?)"
        )


async def _turn_payload(turn: "_AgentTurn", plan, agent_key: str, driver_session_id: str) -> dict:
    """The /followup payload — _build_turn_payload is the one contract. The AI
    endpoint and key, the session title model and the web-search provider +
    credential are read per turn (admin override, else env), so an admin
    change reaches the harness on the next turn without a restart."""
    ai = await settings.get_all(["AI_API_URL", "AI_API_KEY"])
    # KeyError on the provider row is unreachable: `choices` refuses an
    # off-list value at import (env) and at PUT (override); the migration
    # deletes a persisted pre-change `off`.
    provider = await settings.get("WEB_SEARCH_PROVIDER")
    pin, credential_key = WEB_SEARCH_PROVIDERS[provider]
    if provider == "perplexity":
        pin = PERPLEXITY_SEARCH_PINS[await settings.get("PERPLEXITY_SEARCH_TYPE")]
    config = await settings.get_all(
        ["CHAT_TITLE_MODEL", credential_key])
    return _build_turn_payload(
        model=turn.model, system_prompt=plan.system_prompt,
        tools=plan.tools, agent_key=agent_key, apply_mode=plan.apply_mode,
        session_id=driver_session_id,
        user_id=turn.user.get("user_id", ""),
        project_id=turn.project_id or "",
        document_id=turn.session.get("document_id"),
        prompt=plan.prompt, assistant_msg_id=turn.assistant_msg_id,
        time_stamps=plan.time_stamps,
        skills=plan.skill_docs, region=plan.region,
        reasoning_effort=turn.reasoning_effort,
        ai_api_url=ai["AI_API_URL"], ai_api_key=ai["AI_API_KEY"],
        title_model=config["CHAT_TITLE_MODEL"],
        web_search_provider=pin,
        web_search_credential=config[credential_key],
    )


async def _accept_harness_turn(
    turn: "_AgentTurn", driver_session_id: str, lock_token: str,
) -> tuple[dict, asyncio.Task]:
    """The accepted-turn core: fan-out, prep, standing key, bind, preamble,
    followup. Returns (followup reply, heartbeat task) — the heartbeat is the
    caller's failure-path cleanup handle; on success it lives until the
    channel's on_end tears it down with the lock."""
    session_id = turn.session_id
    await _ensure_fanout_or_refuse(turn, session_id, driver_session_id)

    plan = await prepare_agent_turn(
        session=turn.session, body=turn.body, ctx=turn.ctx,
        project_id=turn.project_id, user=turn.user, toolset=turn.toolset,
        capability_is_mutating=turn.capability_is_mutating,
    )

    from agent.keys import get_or_create_session_agent_key

    agent_key = await get_or_create_session_agent_key(
        turn.user.get("user_id", ""), turn.project_id or "", session_id,
    )

    channel = get_driver_channel()
    heartbeat = asyncio.ensure_future(
        _heartbeat_turn_lock(session_id, lock_token))

    async def _on_turn_end() -> None:
        # The lock's release seam (driver.channel._TurnBind ARCH): fires
        # once when the channel closes THIS turn.
        await _teardown_turn_lock(session_id, lock_token, heartbeat)

    channel.bind_turn(
        driver_session_id,
        assistant_msg_id=turn.assistant_msg_id,
        sources=turn.ctx.sources or [],
        user_id=turn.user.get("user_id", ""),
        on_end=_on_turn_end,
    )
    # Preamble BEFORE the followup: the listener queue is the one
    # ordering point, so the browser can never see a driver frame ahead
    # of the ids/sources/warnings it belongs after.
    channel.emit_frames(driver_session_id, _preamble_frames(turn))
    payload = await _turn_payload(turn, plan, agent_key, driver_session_id)
    reply = await timeline.post_followup(payload, line=turn.line)
    return reply, heartbeat


async def _refuse_unconfigured_line(turn, db, lock_token: str, session_id: str) -> None:
    """The line-unavailable refusal twin of the turn path's 503 arm, minus the
    frames: no channel exists to carry them, so the HTTP status is the whole
    signal. Checked BEFORE ensure_fanout — an unconfigured line would
    otherwise hang the subscribe ack wait."""
    await _delete_empty_assistant(db, turn.assistant_msg_id)
    await _teardown_turn_lock(session_id, lock_token, None)
    raise HTTPException(
        status_code=503,
        detail=(
            "Agent line unavailable — the agent service is disabled. "
            "Enable the agent service to chat."
        ),
    )


async def run_harness_turn(turn: "_AgentTurn", db, lock_token: str) -> JSONResponse:
    """Drive one driver-owned turn. Called from create_completion
    under the turn lock, after the shared setup; the lock's release moves to
    the channel's on_end (see module ARCH) on the accepted path."""
    session_id = turn.session_id
    driver_session_id = turn.session.get("compacted_from") or session_id

    if turn.line is None:
        await _refuse_unconfigured_line(turn, db, lock_token, session_id)

    heartbeat: asyncio.Task | None = None

    async def _fail(**kw) -> None:
        await _fail_harness_turn(
            turn, db, lock_token, heartbeat, driver_session_id, **kw)

    try:
        reply, heartbeat = await _accept_harness_turn(
            turn, driver_session_id, lock_token)
    except DriverTurnBusy:
        await _fail(
            message="A turn is already in progress on this chat.",
            status=409, reason="driver_turn_busy", source="followup")
    except DriverSecretMismatch:
        await _fail(
            message=DriverSecretMismatch.CAUSE,
            status=502, reason="driver_secret_mismatch", source="followup")
    except DriverLineUnreachable as exc:
        await _fail(
            message=(
                f"Agent line unavailable — the {exc.line_name} service is "
                "not reachable. Enable the agent service to chat."
            ),
            status=502, reason="agent_line_unreachable", source="setup")
    except Exception:
        logger.exception("harness turn setup failed session=%s", session_id)
        await _fail(
            message="Agent setup failed — please try again",
            status=500, reason="agent_setup_failed", source="setup")

    return JSONResponse({
        "accepted": True,
        "user_msg_id": turn.user_msg_id,
        "assistant_msg_id": turn.assistant_msg_id,
        "dsh_session_id": reply.get("dsh_session_id"),
    })
