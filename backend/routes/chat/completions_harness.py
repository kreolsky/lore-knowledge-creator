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

# ARCH: the default `main-*` preload has ONE owner — `_preload_default_skills`
# here. It appends the MASK to the persona's preload list unless the persona's
# frontmatter says `preload_defaults: false`; the plugin expands it against
# the catalog it resolved. The backend reads no list's contents and no skill
# document, so every persona that predates the `main-*` skills keeps its
# document tools with no migration: it simply carries the mask like a chat
# with no persona at all.
"""
import logging
from typing import TYPE_CHECKING

import driver.timeline as timeline
import mcp_servers
import settings
from agent_config_load import instance_skill_disabled
from agent_skills import DEFAULT_SKILLS_MASK
from chat_auto_recall import build_recall_block
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
from persona_settings import skill_hidden_by_persona

from db import get_db
from routes.chat.completions_turn import prepare_agent_turn
from routes.chat.fanout import ensure_fanout

if TYPE_CHECKING:  # no runtime import — completions imports THIS module lazily
    from routes.chat.completions import _AgentTurn

logger = logging.getLogger(__name__)

#: Why memory is off for a persona whose `allow` / `deny` hides chat-recall.
CHAT_RECALL_HIDDEN = "chat-recall hidden by this persona"


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
        skill_allow=plan.skill_allow, skill_deny=plan.skill_deny,
        recall_block=plan.recall_block,
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


# The chat row's first user messages an auto-recall is attempted on. Operator
# ruling 2026-11-26: the simple version — once one of them lands, the row never
# recalls again; a search per meaningful message is the concept's next step.
RECALL_FIRST_USER_MESSAGES = 5


def _turn_warning(turn: "_AgentTurn", code: str, detail: str | None) -> None:
    """A persona-setting complaint as a turn warning (nothing when there is none)."""
    if detail:
        turn.warnings.append({"code": code, "id": None, "detail": detail})


def _preload_default_skills(plan) -> None:
    """The default `main-*` skills join the persona's preload list (the ONE
    owner — module ARCH), as the mask the plugin expands. A persona that
    lists the mask itself does not get it twice."""
    if plan.preload_defaults and DEFAULT_SKILLS_MASK not in plan.preload_skills:
        plan.preload_skills.append(DEFAULT_SKILLS_MASK)


def _memory_blocker(plan) -> str | None:
    """Why the persona's own settings turn memory off although it asks for it —
    chat-recall hidden by its `allow` / `deny` — or None. Memory is read through
    chat-recall's tools, so a persona hiding the skill gets no half-memory.

    WHY a one-name matcher here (persona_settings.skill_hidden_by_persona): the
    plugin drops the skill from the catalog, but the memory build has to stop
    before the turn leaves — and this expands no mask against a catalog."""
    if skill_hidden_by_persona(CHAT_RECALL_SKILL, plan.skill_allow, plan.skill_deny):
        return CHAT_RECALL_HIDDEN
    return None


def _memory_off(plan, sid: str, stored: dict | None, off: dict, persona_id: str, reason: str) -> None:
    """Memory is off although the persona asks for it: log why, and record the
    off state with `reason` on the chat row unless it already holds exactly it
    for this persona (the chat's memory icon turns red with the reason)."""
    logger.warning("chat memory off session=%s persona=%r: %s", sid, persona_id, reason)
    if not stored or stored.get("error") != reason or _persona_switched(stored, persona_id):
        plan.memory_record = off | {"error": reason}


async def _attach_chat_memory(turn: "_AgentTurn", plan) -> None:
    """Memory ON (the persona's budget) and chat-recall served (enabled by the
    admin, not hidden by the persona) ⇒ preload chat-recall every turn and, until
    the chat has memory, build the block and a pending record (run_harness_turn
    writes it once the send is accepted). A broken budget or a hidden chat-recall
    records why memory is off; auto-recall gets its own attempt.

    WHY a build failure does not fail the turn: the block is agent-only context
    — the chat works without it, and the exception is logged.
    """
    stored = turn.session.get("memory")
    persona_id = turn.session.get("system_prompt_id") or ""
    off = {"message_id": turn.user_msg_id, "chats": [], "error": None, "persona_id": persona_id}
    sid = turn.session_id
    # Every turn, memory on or off: a persona asking for auto-recall it cannot get says so.
    _turn_warning(turn, "persona_recall_budget", plan.recall_warning)
    if plan.memory_error is not None:
        _memory_off(plan, sid, stored, off, persona_id, plan.memory_error)
        return
    # WHY memory-off records a switch: the record then names the off persona,
    # so a switch BACK rebuilds (chats = [] rebuilds by itself).
    if plan.chat_memory_budget <= 0:
        logger.info("chat memory skipped session=%s persona=%r: no chat_memory_budget "
                    "(Default, persona not under Personas, or budget absent)", sid, persona_id)
        if _persona_switched(stored, persona_id):
            plan.memory_record = off
        return
    if (reason := _memory_blocker(plan)) is not None:
        _memory_off(plan, sid, stored, off, persona_id, reason)
        return
    db = await get_db()
    # Admin off-switch, shared with labelling: topics without the tools to
    # open them would mislead.
    if await instance_skill_disabled(db, CHAT_RECALL_SKILL):
        logger.warning("chat memory skipped session=%s persona=%r: chat-recall disabled by admin",
                       sid, persona_id)
        if _persona_switched(stored, persona_id):
            plan.memory_record = off
        return
    if CHAT_RECALL_SKILL not in plan.preload_skills:
        plan.preload_skills.append(CHAT_RECALL_SKILL)
    _turn_warning(turn, "persona_memory_order", plan.memory_order_warning)
    if _memory_needs_build(stored) or _persona_switched(stored, persona_id):
        await _build_chat_memory(turn, plan, db, persona_id, off)
    else:
        logger.info("chat memory kept session=%s persona=%r: already received %d chats",
                    sid, persona_id, len(stored.get("chats") or []))
    await _attach_auto_recall(turn, plan, db, persona_id)


async def _attach_auto_recall(turn: "_AgentTurn", plan, db, persona_id: str) -> None:
    """Auto-recall, memory ON: on the chat row's first RECALL_FIRST_USER_MESSAGES
    user messages, until one lands for the current persona — the block into
    `plan`, the pending record written by run_harness_turn once the send is
    accepted (chat_auto_recall.py decides WHAT is recalled, this decides WHEN).

    WHY a failed recall does not fail the turn: the block is agent-only context,
    and a turn warning says it did not happen — the person must not read
    silence as "never discussed". The row stays unrecorded, so the next turn
    tries again.
    """
    if plan.chat_recall_budget <= 0:
        return
    stored = turn.session.get("recall")
    sid = turn.session_id
    if stored and stored.get("persona_id") == persona_id:
        logger.info("chat auto-recall kept session=%s persona=%r: already landed for %d chats",
                    sid, persona_id, len(stored.get("chats") or []))
        return
    # The send's own row is minted at the claim, so the row's user messages are
    # exactly the earlier ones — this send is the next.
    rows = await db.query(
        "SELECT count() AS n FROM messages WHERE chat_id = $sid AND role = 'user' "
        "AND deleted_at IS NONE GROUP ALL",
        {"sid": sid},
    ) or []
    if int((rows[0] or {}).get("n") or 0) >= RECALL_FIRST_USER_MESSAGES:
        logger.info("chat auto-recall skipped session=%s: past its first %d user messages",
                    sid, RECALL_FIRST_USER_MESSAGES)
        return
    cap = await settings.get("CHAT_MEMORY_MAX_TOKENS")
    if plan.chat_recall_budget > cap:
        logger.warning("chat auto-recall: persona %s budget %d above the admin cap — clamped to %d",
                       persona_id, plan.chat_recall_budget, cap)
    try:
        result = await build_recall_block(
            db, session_id=sid, session=turn.session,
            user_id=turn.user.get("user_id", ""), project_id=turn.project_id or "",
            persona_id=persona_id, text=turn.body.messages[-1].content,
            budget=min(plan.chat_recall_budget, cap),
        )
    except Exception:  # noqa: BLE001 — the embedding call, the 3 s bound, a query
        logger.exception("chat auto-recall failed session=%s", sid)
        turn.warnings.append({"code": "persona_recall_failed", "id": None,
                              "detail": "Earlier conversations close to this message could not be recalled"})
        return
    plan.recall_block = result.block
    if result.block:
        plan.recall_record = {"message_id": turn.user_msg_id, "persona_id": persona_id,
                             "chats": result.chats}


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
    logger.info("chat memory built session=%s persona=%r chats=%d block_chars=%d",
                turn.session_id, persona_id, len(shown), len(plan.memory_block or ""))


# The chat row's block records and the states in which each is rewritten
# beside a persona change. `memory`: an errored or chat-less record — exactly
# the states that rebuild the block. `recall`: none — a recall record is
# written only with hits, so those two clauses could never hold for it.
_RECORD_REBUILDS = {
    "memory": "OR {f}.error IS NOT NONE OR array::len({f}.chats ?? []) = 0 ",
    "recall": "",
}


async def _record_chat_block(
    session_id: str, field: str, record: dict | None, stored: dict | None,
) -> dict | None:
    """Store `record` on the chat row's `field` — `memory` or `recall` — unless
    the row already holds a record built for the record's persona; returns the
    row's record after the write (`stored`, the record read with the session,
    when there is nothing to write). `field` is a key of _RECORD_REBUILDS: the
    lookup runs before any SQL is built, so the field name is never free text.

    WHY conditional: of two quick sends that both built a block, only the
    first records — its message_id is the turn the plugin injected into.
    WHY a failed write does not fail the send: the turn is already queued — an
    error answer would make the browser resend it; the next turn rebuilds and
    records again.
    """
    if record is None:
        return stored
    db = await get_db()
    rebuild = _RECORD_REBUILDS[field].format(f=field)
    try:
        rows = await db.query(
            f"UPDATE type::record('chat_sessions', $sid) SET {field} = $r "
            f"WHERE {field} IS NONE {rebuild}"
            f"OR ({field}.persona_id IS NOT NONE AND {field}.persona_id != $p) RETURN AFTER",
            {"sid": session_id, "r": record, "p": record.get("persona_id")},
        ) or []
        if not rows:
            logger.info("chat %s record kept session=%s: the row already holds one", field, session_id)
            rows = await db.query(
                f"SELECT {field} FROM type::record('chat_sessions', $sid)", {"sid": session_id},
            ) or []
        else:
            logger.info("chat %s recorded session=%s chats=%d error=%r", field, session_id,
                        len(record.get("chats") or []), record.get("error"))
    except Exception:
        logger.exception("chat %s record write failed session=%s", field, session_id)
        return stored
    return rows[0].get(field) if rows else None


async def _record_turn_blocks(
    turn: "_AgentTurn", memory_record: dict | None, recall_record: dict | None,
) -> dict:
    """Store the turn's pending records on the chat row, as `{memory, recall}` —
    each the row's record after the write.

    WHY after the accept, never inside it: a send refused by the driver after
    a write would leave a record that stops every later rebuild — the agent
    would never get the block while the row claims it did.
    """
    out: dict = {}
    for field, record in (("memory", memory_record), ("recall", recall_record)):
        out[field] = await _record_chat_block(
            turn.session_id, field, record, turn.session.get(field))
    return out


async def _accept_harness_turn(
    turn: "_AgentTurn", driver_session_id: str,
) -> tuple[dict, dict | None, dict | None]:
    """The accepted-send core: fan-out, prep, standing key, followup.
    Returns the followup reply and the pending memory and recall records."""
    session_id = turn.session_id
    await _ensure_fanout_or_refuse(turn, session_id, driver_session_id)
    await _stamp_origin_persona(session_id, turn.session.get("system_prompt_id"))

    plan = await prepare_agent_turn(
        session=turn.session, body=turn.body, vision_ok=turn.vision_ok,
        project_id=turn.project_id, user=turn.user, toolset=turn.toolset,
        capability_is_mutating=turn.capability_is_mutating,
    )
    _preload_default_skills(plan)
    await _attach_chat_memory(turn, plan)

    from agent.keys import get_or_create_session_agent_key

    agent_key = await get_or_create_session_agent_key(
        turn.user.get("user_id", ""), turn.project_id or "", session_id,
    )

    payload = await _turn_payload(turn, plan, agent_key, driver_session_id)
    return (await timeline.post_followup(payload, line=turn.line),
            plan.memory_record, plan.recall_record)


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
        reply, memory_record, recall_record = await _accept_harness_turn(turn, driver_session_id)
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

    records = await _record_turn_blocks(turn, memory_record, recall_record)
    return JSONResponse({
        "accepted": True,
        "user_msg_id": turn.user_msg_id,
        "assistant_msg_id": turn.assistant_msg_id,
        "dsh_session_id": reply.get("dsh_session_id"),
        "message_id": reply.get("message_id"),
        "memory": records["memory"],
        "recall": records["recall"],
    })
