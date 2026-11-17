"""The driver-owned turn path.

Part of the chat-completions system — the SYSTEM marker stays in
completions.py (the mechanical-split precedent: completions_turn). This
module owns ONE transport arm.

The driver owns every turn. This runs AFTER the shared setup (validation,
vision read — `_prepare_agent_turn_setup` in completions.py) and hands the send
over: ensure_fanout (the channel + the project-WS pump), the per-session
STANDING agent key (agent.keys.get_or_create_session_agent_key), and POST
/followup, which queues the send in the session's dsh inbox. The HTTP answer
is JSON — the frames live on the project WS (routes.chat.fanout) and the
reload path.

# ARCH: the preamble facts (the row ids, the user's text and attachments,
# context warnings) ride the send's prep to the driver and come back
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
import mcp_servers
import settings
from agent_config_load import instance_skill_disabled
from chat_memory import build_memory_block
from chat_sessions.recall import CHAT_RECALL_SKILL
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

from db import get_db
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
    # The turn's MCP servers are the SENDER's (SYSTEM: mcp-servers): rendered
    # per user from their own stored keys, re-resolved every turn so a key
    # change, a toggle or a revoked server lands on the next message. Their
    # names ride the system prompt (prompt_section: "" for a user with no
    # servers, so everyone else's prompt stays byte-identical).
    mcp_entries = await mcp_servers.render_for_user(await get_db(), turn.user)
    return _build_turn_payload(
        model=turn.model,
        system_prompt=plan.system_prompt + mcp_servers.prompt_section(mcp_entries),
        tools=plan.tools, agent_key=agent_key, apply_mode=plan.apply_mode,
        session_id=driver_session_id,
        user_id=turn.user.get("user_id", ""),
        project_id=turn.project_id or "",
        document_id=turn.session.get("document_id"),
        prompt=plan.prompt, assistant_msg_id=turn.assistant_msg_id,
        time_stamps=plan.time_stamps,
        skills=plan.skill_docs,
        preload_skills=plan.preload_skills, memory_block=plan.memory_block,
        memory_persona=turn.session.get("system_prompt_id") or "",
        reasoning_effort=turn.reasoning_effort,
        ai_api_url=ai["AI_API_URL"], ai_api_key=ai["AI_API_KEY"],
        title_model=config["CHAT_TITLE_MODEL"],
        web_search_provider=pin,
        web_search_credential=config[credential_key],
        mcp_servers=mcp_entries,
    ) | _claim_facts(turn)


def _claim_facts(turn: "_AgentTurn") -> dict:
    """The facts `lore/claim` returns verbatim: the plugin keeps them with
    the queued message, and the channel mints the row pair and the preamble
    from them at claim time (the dsh message holds the assembled prompt, not
    the user's own text, so a row cannot be rebuilt from it)."""
    last = turn.body.messages[-1]
    return {
        "user_msg_id": turn.user_msg_id,
        "user_text": last.content,
        "images": last.images or [],
        # getattr: see _prompt_text — stubs of a message predate `files`.
        "files": [f.model_dump() for f in getattr(last, "files", None) or []],
        "warnings": list(turn.warnings),
    }


async def _stamp_origin_persona(session_id: str, persona_id: str | None) -> None:
    """Stamp the persona this chat STARTED with ("" = Default) on an unbound row.

    INVARIANT(persisted): origin_persona_id is stamped at the first turn, then
    follows every persona change of the conversation.
    Why: operator ruling — persona and memory are one choice.
    A system_prompt_id PATCH writes it (chat_sessions/update.py) and is never
    NONE, so the WHERE never overwrites it. Derived rows (branch, compaction
    continuation) copy it at their mint, so their first turn never re-stamps.
    """
    db = await get_db()
    await db.query(
        "UPDATE type::record('chat_sessions', $sid) SET origin_persona_id = $p "
        "WHERE origin_persona_id IS NONE",
        {"sid": session_id, "p": persona_id or ""},
    )


def _memory_needs_build(stored: dict | None) -> bool:
    """True until the chat row records memory that carried chats.

    WHY `chats == []` rebuilds too: an empty block injected nothing (the
    plugin skips ''), so a persona switched to mid-chat, or labelling that
    finished since, still brings memory at a later turn. A persona switch
    rebuilds via `_persona_switched`. The plugin keeps at most one
    `lore-memory` node per surface and replaces it only on a persona change
    (INVARIANT in harness-driver/plugin/src/preload.ts), so a rebuild never
    doubles it.
    """
    return not stored or stored.get("error") is not None or not stored.get("chats")


def _persona_switched(stored: dict | None, persona_id: str) -> bool:
    """True when the record was built for a persona other than the current one.
    A record without `persona_id` (written before the tag) counts as the same
    persona — the plugin never swaps an untagged block either."""
    recorded = (stored or {}).get("persona_id")
    return recorded is not None and recorded != persona_id


async def _attach_chat_memory(turn: "_AgentTurn", plan) -> None:
    """Memory ON (the persona's budget) and chat-recall enabled ⇒ preload
    chat-recall every turn and, until the chat has received memory, build the
    block and a pending record (written by run_harness_turn once the send is
    accepted). A persona whose budget is broken records why memory is off.

    WHY a build failure does not fail the turn: the block is agent-only
    context — the chat works without it, and the exception is logged.
    """
    stored = turn.session.get("memory")
    persona_id = turn.session.get("system_prompt_id") or ""
    off = {"message_id": turn.user_msg_id, "chats": [], "error": None, "persona_id": persona_id}
    if plan.memory_error is not None:
        if not stored or stored.get("error") != plan.memory_error \
                or _persona_switched(stored, persona_id):
            plan.memory_record = off | {"error": plan.memory_error}
        return
    # WHY memory-off records a switch: the record then names the off persona,
    # so a switch BACK rebuilds (chats = [] rebuilds by itself).
    if plan.chat_memory_budget <= 0:
        if _persona_switched(stored, persona_id):
            plan.memory_record = off
        return
    db = await get_db()
    # Admin off-switch, shared with labelling: topics without the tools to
    # open them would mislead.
    if await instance_skill_disabled(db, CHAT_RECALL_SKILL):
        if _persona_switched(stored, persona_id):
            plan.memory_record = off
        return
    if CHAT_RECALL_SKILL not in plan.preload_skills:
        plan.preload_skills.append(CHAT_RECALL_SKILL)
    if plan.memory_order_warning:
        turn.warnings.append({"code": "persona_memory_order", "id": None,
                             "detail": plan.memory_order_warning})
    if _memory_needs_build(stored) or _persona_switched(stored, persona_id):
        await _build_chat_memory(turn, plan, db, persona_id, off)


async def _build_chat_memory(turn: "_AgentTurn", plan, db, persona_id: str, off: dict) -> None:
    """Build the persona's block into `plan` with its pending record (`off` + chats)."""
    cap = await settings.get("CHAT_MEMORY_MAX_TOKENS")
    if plan.chat_memory_budget > cap:
        logger.warning("chat memory: persona %s budget %d above the admin cap — clamped to %d",
                       persona_id, plan.chat_memory_budget, cap)
    try:
        plan.memory_block, shown = await build_memory_block(
            db, session_id=turn.session_id, session=turn.session,
            user_id=turn.user.get("user_id", ""), project_id=turn.project_id or "",
            persona_id=persona_id, budget=min(plan.chat_memory_budget, cap),
            order=plan.chat_memory_order,
        )
    except Exception:
        logger.exception("chat memory block failed session=%s", turn.session_id)
        return
    plan.memory_record = off | {"chats": shown}


async def _record_chat_memory(
    session_id: str, record: dict | None, stored: dict | None,
) -> dict | None:
    """Store `record` on the chat row unless it already holds memory with
    chats for the record's persona; returns the row's record after the write
    (`stored`, the record read with the session, when there is nothing to write).

    WHY conditional: of two quick sends that both built a block, only the
    first records — its message_id is the turn the plugin injected into.
    WHY a failed write does not fail the send: the turn is already queued — an
    error answer would make the browser resend it; the next turn rebuilds and
    records again.
    """
    if record is None:
        return stored
    db = await get_db()
    try:
        rows = await db.query(
            "UPDATE type::record('chat_sessions', $sid) SET memory = $m "
            "WHERE memory IS NONE OR memory.error IS NOT NONE "
            "OR array::len(memory.chats ?? []) = 0 "
            "OR (memory.persona_id IS NOT NONE AND memory.persona_id != $p) RETURN AFTER",
            {"sid": session_id, "m": record, "p": record.get("persona_id")},
        ) or []
        if not rows:
            rows = await db.query(
                "SELECT memory FROM type::record('chat_sessions', $sid)", {"sid": session_id},
            ) or []
    except Exception:
        logger.exception("chat memory record write failed session=%s", session_id)
        return stored
    return rows[0].get("memory") if rows else None


async def _accept_harness_turn(
    turn: "_AgentTurn", driver_session_id: str,
) -> tuple[dict, dict | None]:
    """The accepted-send core: fan-out, prep, standing key, followup.
    Returns the followup reply and the pending memory record."""
    session_id = turn.session_id
    await _ensure_fanout_or_refuse(turn, session_id, driver_session_id)
    await _stamp_origin_persona(session_id, turn.session.get("system_prompt_id"))

    plan = await prepare_agent_turn(
        session=turn.session, body=turn.body, vision_ok=turn.vision_ok,
        project_id=turn.project_id, user=turn.user, toolset=turn.toolset,
        capability_is_mutating=turn.capability_is_mutating,
    )
    await _attach_chat_memory(turn, plan)

    from agent.keys import get_or_create_session_agent_key

    agent_key = await get_or_create_session_agent_key(
        turn.user.get("user_id", ""), turn.project_id or "", session_id,
    )

    payload = await _turn_payload(turn, plan, agent_key, driver_session_id)
    return await timeline.post_followup(payload, line=turn.line), plan.memory_record


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
        reply, memory_record = await _accept_harness_turn(turn, driver_session_id)
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

    # WHY after the accept, never inside it: a send refused by the driver
    # after a write would leave a record that stops every later rebuild — the
    # agent would never get the block while the row claims it did.
    memory = await _record_chat_memory(
        session_id, memory_record, turn.session.get("memory"))
    return JSONResponse({
        "accepted": True,
        "user_msg_id": turn.user_msg_id,
        "assistant_msg_id": turn.assistant_msg_id,
        "dsh_session_id": reply.get("dsh_session_id"),
        "message_id": reply.get("message_id"),
        "memory": memory,
    })
