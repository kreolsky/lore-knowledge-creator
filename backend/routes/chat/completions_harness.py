"""The driver-owned turn path.

Part of the chat-completions system — the SYSTEM marker stays in
completions.py (the mechanical-split precedent: completions_turn). This
module owns ONE transport arm.

The driver owns every turn. This runs AFTER the shared setup (validation,
context — `_prepare_agent_turn_setup` in completions.py) and hands the send
over: ensure_fanout (the channel + the project-WS pump), the per-session
STANDING agent key (agent.keys.get_or_create_session_agent_key), and POST
/followup, which queues the send in the session's dsh inbox. The HTTP answer
is JSON — the frames live on the project WS (routes.chat.fanout) and the
reload path.

# ARCH: the preamble facts (the row ids, the user's text and attachments,
# sources, context warnings) ride the send's prep to the driver and come back
# in `lore/claim` when dsh starts the turn; the channel mints the row pair and
# emits the preamble there (driver.channel `_on_claim`). A refused send emits
# NO frames — another queued turn may be running, and a terminal frame would
# close ITS browser registration; the HTTP error status is the whole signal.

# ARCH: the turn's session_id on the wire is the DRIVER session id
# (compacted_from or the chat id) — the compaction continuation contract
# shared by the turn path's both arms.
"""
import logging
from typing import TYPE_CHECKING

import driver.timeline as timeline
import settings
from driver.client import (
    PERPLEXITY_SEARCH_PINS,
    WEB_SEARCH_PROVIDERS,
    DriverLineUnreachable,
    DriverSecretMismatch,
    _build_turn_payload,
)
from driver.timeline import DriverTurnRefused
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routes.chat.completions_turn import prepare_agent_turn
from routes.chat.fanout import ensure_fanout

if TYPE_CHECKING:  # no runtime import — completions imports THIS module lazily
    from routes.chat.completions import _AgentTurn

logger = logging.getLogger(__name__)


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
    turn, *, message: str, status: int, reason: str, source: str,
) -> None:
    """The terminal failure ladder: telemetry and the named HTTP refusal
    (nothing was queued, no row exists, no frame is emitted — module ARCH)."""
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
    change reaches the harness on the next turn without a restart.

    WHY frozen at send: a queued message runs with the system prompt, model
    and context the user had when sending, even if an earlier queued turn
    edited a document — the prep is built here and never rebuilt at claim.
    Cost: a queued turn reasons over a pre-edit context snapshot until it
    re-reads via tools."""
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
    ) | _claim_facts(turn)


def _claim_facts(turn: "_AgentTurn") -> dict:
    """The facts `lore/claim` returns verbatim: the plugin keeps them with
    the queued message, and the channel mints the row pair and the preamble
    from them at claim time (the dsh message holds the merged context images,
    so a row cannot be rebuilt from it)."""
    last = turn.body.messages[-1]
    return {
        "user_msg_id": turn.user_msg_id,
        "user_text": last.content,
        "images": last.images or [],
        # getattr: see _prompt_text — stubs of a message predate `files`.
        "files": [f.model_dump() for f in getattr(last, "files", None) or []],
        "sources": turn.ctx.sources or [],
        "warnings": list(turn.ctx.warnings),
    }


async def _accept_harness_turn(
    turn: "_AgentTurn", driver_session_id: str,
) -> dict:
    """The accepted-send core: fan-out, prep, standing key, followup.
    Returns the followup reply."""
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

    payload = await _turn_payload(turn, plan, agent_key, driver_session_id)
    return await timeline.post_followup(payload, line=turn.line)


def _refuse_unconfigured_line() -> None:
    """The line-unavailable refusal: no channel exists to carry frames, so
    the HTTP status is the whole signal. Checked BEFORE ensure_fanout — an
    unconfigured line would otherwise hang the subscribe ack wait."""
    raise HTTPException(
        status_code=503,
        detail=(
            "Agent line unavailable — the agent service is disabled. "
            "Enable the agent service to chat."
        ),
    )


async def run_harness_turn(turn: "_AgentTurn") -> JSONResponse:
    """Hand one send to the driver. Called from create_completion after the
    shared setup; the turn runs when dsh claims the queued message."""
    session_id = turn.session_id
    driver_session_id = turn.session.get("compacted_from") or session_id

    if turn.line is None:
        _refuse_unconfigured_line()

    async def _fail(**kw) -> None:
        await _fail_harness_turn(turn, **kw)

    try:
        reply = await _accept_harness_turn(turn, driver_session_id)
    except DriverTurnRefused as exc:
        await _fail(
            message=str(exc), status=422,
            reason="driver_turn_refused", source="followup")
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
        "message_id": reply.get("message_id"),
    })
