"""Session CRUD routes and the session access gates for chat sessions."""
# SYSTEM: chat-sessions — session CRUD, access gating, LLM config inheritance, title pin
# The list-sessions read path (candidate load, ref/title/preview/last-activity
# aggregates, last-message sort, piggyback) lives in `sessions_list`. The CRUD
# handlers here are guard + command: the bodies (inheritance, the note-link
# strip, the wire shape) live in `backend/chat_sessions/`. Access gates and the
# harness teardown on delete stay here. Session TITLES are minted by the harness
# titler and relayed as `session/title` (driver.client → driver.persistence
# ._persist_session_title); a PATCH only pins user renames.

import logging

import settings
from chat_sessions.create import create_session_command
from chat_sessions.delete import delete_session_command
from chat_sessions.update import update_session_command
from fastapi import Depends, HTTPException
from surrealdb import AsyncSurreal

from access import get_document_access, get_project_access
from auth import get_current_user
from db import fetch_one, get_db
from models import SessionCreate, SessionUpdate
from routes.chat._router import router

logger = logging.getLogger(__name__)


def _assert_agent_full_access(access: str | None) -> None:
    """Single source of the "agent mode requires full project access" policy.

    # ARCH: every AI chat runs the mutating Tool-API surface under the
    # session's standing agent key, so full access is required at CREATE —
    # before any turn can mutate through it.
    """
    if access != "full":
        raise HTTPException(
            status_code=403, detail="Full project access required for agent mode",
        )


async def _validate_reasoning_effort(model: str, effort: str) -> None:
    """400 unless `effort` is in `model`'s advertised reasoning-effort list.

    Single source of the PATCH guard: reads the
    TTL-cached /capabilities map (models_catalog.advertised_effort_levels) —
    the same map the picker renders from, so what the UI offers and what the
    guard accepts cannot drift. None (unknown model / gateway without the
    endpoint / non-reasoning model) rejects every non-null value.
    """
    from routes.chat.models_catalog import advertised_effort_levels

    levels = await advertised_effort_levels(model)
    if levels is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' does not advertise reasoning effort levels",
        )
    if effort not in levels:
        raise HTTPException(
            status_code=400,
            detail=(
                f"reasoning_effort '{effort}' is not allowed for model '{model}' "
                f"(allowed: {', '.join(levels)})"
            ),
        )


async def _require_session_access(session_id: str, user: dict) -> dict:
    """Fetch chat session and verify access. Returns session dict.

    AI chats (is_note=false): owner-only AND project member. Returns 404 to
    non-owners and to ex-members (covers "not found", "not yours", and
    "revoked" without leaking session existence).

    Note-chats (is_note=true): visible to any project member with at least
    commentator access. The note-chat is a shared comment thread, not a
    private conversation.
    """
    session = await fetch_one("chat_sessions", session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    if session.get("is_note") is True:
        did = session.get("document_id", "")
        access = (
            await get_document_access(did, user)
            if did else await get_project_access(session.get("project_id", ""), user)
        )
        if access is None or access == "readonly":
            raise HTTPException(status_code=404, detail="Chat session not found")
        return session
    if session.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=404, detail="Chat session not found")
    # WHY: re-validate project membership for AI chats on EVERY request. Why: a user
    # removed from a project (or whose access was lowered) must not keep full AI-chat
    # access merely because they still own the session — they could drive
    # completions against documents they can still name. get_project_access reads
    # project_members fresh per
    # request, so this closes the data-exfiltration vector for removed/revoked users.
    # 404 (not 403) mirrors both branches above — avoids leaking session existence.
    access = await get_project_access(session.get("project_id", ""), user)
    if access is None:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


async def _guard_create_access(body: SessionCreate, user: dict) -> None:
    """404 without project access; 403 for a readonly note or a non-full AI chat."""
    target_did = body.reference_id or body.document_id
    access = (
        await get_document_access(target_did, user)
        if target_did else await get_project_access(body.project_id, user)
    )
    if access is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if body.is_note and access == "readonly":
        raise HTTPException(status_code=403, detail="Commentator access required for note-chats")
    # INVARIANT(security): every AI chat is an agent chat — there is no second AI
    # Why: there is no Ask/read-only line. Full access is required at create, not
    # just at apply.
    # Why: this is a repeatedly-regressing area — the access gate was once scoped
    # to `mode == 'agent'`, which left a direct-API gap (a non-full user POSTing
    # with mode omitted ran the full agent loop). With the mode axis gone the gate
    # is unconditional for AI chats; the note path is exempt (notes never call the
    # LLM). The frontend blocks non-full creation too; this is the backend half
    # (defense-in-depth, never UI-hiding alone).
    if not body.is_note:
        _assert_agent_full_access(access)


@router.post("/sessions", status_code=201)
async def create_session(body: SessionCreate, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Create a new chat session scoped to a document or reference.

    Note-chats (body.is_note=true) need at least commentator access: they are
    the comment channel, and readonly users cannot start one.
    """
    await _guard_create_access(body, user)
    return await create_session_command(db, body, user["user_id"])


async def _guard_reasoning_effort(session: dict, body: SessionUpdate) -> None:
    """400 when a non-null reasoning_effort is not advertised by the target model."""
    # ARCH: the reasoning-effort guard. Null is
    # ALWAYS legal (Default — and the model-change reset PATCHes
    # {model, reasoning_effort: null} together); a non-null value must be in the
    # TARGET model's advertised list (body.model when the PATCH switches models,
    # else the row's model). Why gate here instead of turn time: a stale level
    # under a non-reasoning model would pass the router untouched and die
    # upstream with an opaque error — the 400 names the constraint while the
    # picker is still on screen. An absent map entry (gateway without
    # /capabilities, unknown model) advertises nothing: non-null is rejected —
    # honest, retryable, never a silent drop.
    # Note sessions never take an LLM turn; the field is silently ignored for
    # them (the context_ids precedent).
    if session.get("is_note") is True or "reasoning_effort" not in body.model_fields_set:
        return
    if body.reasoning_effort is None:
        return
    target_model = (
        body.model or session.get("model")
        or await settings.get("CHAT_MODEL")
    )
    await _validate_reasoning_effort(target_model, body.reasoning_effort)


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: SessionUpdate, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Update session title, model, context, agent_auto (confirm↔auto toggle), or parent document.

    ARCH: there is no in-place `mode` PATCH — the ask↔agent axis is not on the
    wire (every AI chat is an agent chat). The only mode-like write is the
    agent_auto column (the confirm↔auto selector), gated on full access +
    non-note as defense-in-depth.
    """
    session = await _require_session_access(session_id, user)
    await _guard_reasoning_effort(session, body)
    return await update_session_command(db, session_id, session, body, user)


async def _teardown_harness(session_id: str, session: dict) -> None:
    """Reject the session's parked asks, stop its fan-out, revoke its agent key."""
    # WHY: a session delete strands every parked approval ask of that session —
    # the message row is gone and no UI remains to answer, so the delete path
    # tells the driver to reject each parked ask and the dsh turn sees the
    # rejection instead of parking on a question nobody can ever answer.
    # Best-effort: the session is already soft-deleted; a relay failure must not
    # block the DELETE (every ask is still bounded by the driver's approval cap).
    try:
        from routes.chat.verdicts import resolve_session_asks

        await resolve_session_asks(session_id, reason="session_closed")
    except Exception:
        logger.warning(
            "resolve_session_asks failed for session %s", session_id, exc_info=True,
        )
    # WHY: a deleted session's
    # standing identity dies with it — the per-session agent key is REVOKED
    # (blast radius) and the harness fan-out stops (an unsubscribe mid-turn
    # is the channel's client-disconnect semantics: partial product +
    # disconnected halt + best-effort /stop — the driver is not left running
    # a turn nobody reads). Best-effort: the row is already soft-deleted.
    try:
        from agent.keys import revoke_session_agent_key

        from routes.chat.fanout import stop_fanout

        await stop_fanout(
            session_id,
            driver_session_id=session.get("compacted_from") or session_id,
        )
        await revoke_session_agent_key(session_id)
    except Exception:
        logger.warning(
            "harness teardown on session delete failed session=%s",
            session_id, exc_info=True,
        )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Soft-delete a chat session and its messages, then tear down its harness side."""
    session = await _require_session_access(session_id, user)
    await delete_session_command(db, session_id, session)
    await _teardown_harness(session_id, session)
    return {"success": True}
