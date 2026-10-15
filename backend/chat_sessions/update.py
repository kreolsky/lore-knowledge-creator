"""Update a chat session: one UPDATE built field by field, with field-level access."""

from fastapi import HTTPException

from access import get_document_access, get_project_access
from chat_sessions.conversation import conversation_session_ids
from chat_sessions.recall import schedule_chat_index
from chat_sessions.serialize import (
    build_ref_map,
    build_session_ref_map,
    serialize_session,
)
from models import SessionUpdate


async def _resolve_session_scope_access(session: dict, user: dict) -> str | None:
    """Resolve the user's access level for a session's scope (plain resolution).

    Returns the document-scoped level when the session is pinned to a document,
    else the project-scoped level. Resolution-only — does NOT raise on no-access
    (the caller decides how to react). Distinct from the route's
    _require_session_access, which is the 404 access/membership gate that
    ALSO fetches the row; this helper takes an already-fetched session dict.
    """
    did = session.get("document_id", "")
    if did:
        return await get_document_access(did, user)
    return await get_project_access(session.get("project_id", ""), user)


def _llm_config_sets(body: SessionUpdate, sets: list[str], params: dict) -> None:
    """Title (a user rename), model and system prompt."""
    if body.title is not None:
        sets.append("title = $title")
        params["title"] = body.title
        # ARCH: a PATCH title is a USER
        # rename and PINS the row against later automatic title revisions. The
        # harness titler relays its revisions blind (dsh never learns of our
        # renames), so the pin must ride the row — the relay write skips rows
        # with the flag set (driver.persistence._persist_session_title). No
        # unpin path: dsh's refresh() is not wired, matching the first-prompt
        # cadence (a rename is final until a fresh session).
        sets.append("title_user_set = true")
    if body.model is not None:
        sets.append("model = $model")
        params["model"] = body.model
    if "system_prompt_id" in body.model_fields_set:
        if body.system_prompt_id is not None:
            sets.append("system_prompt_id = $system_prompt_id")
            params["system_prompt_id"] = body.system_prompt_id
        else:
            sets.append("system_prompt_id = NONE")


def _ai_only_sets(session: dict, body: SessionUpdate, sets: list[str], params: dict) -> None:
    """reasoning_effort, context_ids and the thread's active-branch pointer;
    all silently ignored for note sessions."""
    if session.get("is_note") is True:
        return
    # WHY: a non-null reasoning_effort reaches this write only after the route's
    # advertised-levels guard (routes/chat/sessions.py); null is always legal.
    if "reasoning_effort" in body.model_fields_set:
        if body.reasoning_effort is None:
            sets.append("reasoning_effort = NONE")
        else:
            sets.append("reasoning_effort = $reasoning_effort")
            params["reasoning_effort"] = body.reasoning_effort
    # ARCH: Unified context_ids — write directly to DB column.
    # Note sessions never have context; the field is silently ignored for them.
    if "context_ids" in body.model_fields_set:
        sets.append("context_document_ids = $context_ids")
        params["context_ids"] = list(body.context_ids or [])
    # ARCH: the ROOT row's last-opened branch
    # pointer. The route guard (routes/chat/sessions.py) has already validated
    # a non-null value names a live branch of this row's thread; null is always
    # legal (clears the pointer back to nothing — the row previews itself).
    if "active_branch_id" in body.model_fields_set:
        if body.active_branch_id is None:
            sets.append("active_branch_id = NONE")
        else:
            sets.append("active_branch_id = $active_branch_id")
            params["active_branch_id"] = body.active_branch_id


async def _agent_flag_sets(
    session: dict, body: SessionUpdate, user: dict, sets: list[str], params: dict,
) -> None:
    """agent_auto (the confirm↔auto selector) and the has_region unpin."""
    # ARCH: there is no in-place `mode` PATCH — the field is not on the wire and
    # ask↔agent switching does not exist (every AI chat is already an agent chat).
    # Only the agent_auto column write (the confirm↔auto selector) remains.
    if "agent_auto" in body.model_fields_set:
        # ARCH (defense-in-depth): agent_auto=true is an auto-apply capability
        # grant that only applies to AI (non-note) sessions. The frontend guards
        # access for agent modes, but the backend is the authority (CLAUDE.md) —
        # never trust the client guard alone. Force false when the row is a note
        # session OR the user lacks full access, so a revoked-access user PATCHing
        # {agent_auto:true} cannot silently re-grant auto-apply.
        # The read keys on the honest is_note axis — the only row shape
        # the grant must not apply to is a note session.
        can_auto = not session.get("is_note")
        if can_auto:
            access = await _resolve_session_scope_access(session, user)
            can_auto = access == "full"
        sets.append("agent_auto = $agent_auto")
        params["agent_auto"] = bool(body.agent_auto) and can_auto
    # ARCH: unpin clears the pinned-region flag so
    # auto-apply is re-enabled and the containment constraint is dropped. A pin is
    # established at create only; a PATCH only ever clears it (has_region=false) —
    # a PATCH setting it true is ignored (no re-pin via PATCH; re-pin = new session).
    if "has_region" in body.model_fields_set:
        sets.append("has_region = $has_region")
        params["has_region"] = False


# ARCH: reparent = re-anchor. The write is
# chat_sessions.document_id — the column the proximity sort and build_ref_map
# read; there is no separate "chat context doc" field. A reference-scoped
# session holds the REFERENCE id in that column, so moving it to a document is
# the same write and the row becomes a plain document chat (reference_id is a
# read-only derivation). Refused (400) for note sessions — a note is anchored by
# a note: link inside its owning document's content, moving the row would
# strand the anchor. A non-null target needs at least commentator access —
# reparenting into a document the user cannot see would hide their own chat
# from them and leak a title into that branch's proximity ranking. The agent's
# target_doc_id pin is NOT moved: the turn guard only trusts a target that is a
# descendant of the (new) document_id, so a stale pin is inert outside the new
# scope.
async def _reparent_sets(
    db, session: dict, body: SessionUpdate, user: dict, sets: list[str], params: dict,
) -> None:
    if "document_id" not in body.model_fields_set:
        return
    if session.get("is_note") is True:
        raise HTTPException(status_code=400, detail="Cannot change parent of a note session")
    if body.document_id is None:
        sets.append("document_id = NONE")
        return
    # INVARIANT: a chat can be moved to any document, never onto a reference.
    # Why: operator ruling — "чат можно передать на любой документ, на референс не надо".
    if body.document_id in await build_ref_map(db, body.document_id):
        raise HTTPException(status_code=400, detail="Cannot move a chat onto a reference")
    target_access = await get_document_access(body.document_id, user)
    if target_access is None or target_access == "readonly":
        raise HTTPException(
            status_code=403, detail="Commentator access required on the target document",
        )
    sets.append("document_id = $document_id")
    params["document_id"] = body.document_id


def _unindexed_sets(session: dict, body: SessionUpdate, sets: list[str], params: dict) -> None:
    """The recall-index opt-out on this row; 400 on a note, null is a no-op."""
    if "unindexed" not in body.model_fields_set or body.unindexed is None:
        return
    # WHY: defense-in-depth like agent_auto — a note is never indexed, so the
    # flag on one is a client defect, refused rather than stored.
    if session.get("is_note") is True:
        raise HTTPException(status_code=400, detail="unindexed does not apply to a note session")
    sets.append("unindexed = $unindexed")
    params["unindexed"] = body.unindexed


async def _propagate_unindexed(db, session_id: str, session: dict, value: bool) -> None:
    """Write the flag to every OTHER row of the conversation."""
    # INVARIANT(security): the flag is one value per conversation — a toggle on
    # one branch writes every branch and compaction continuation.
    # Why: a sibling left unflagged keeps the same transcript searchable.
    ids = await conversation_session_ids(db, session_id, session)
    await db.query(
        "UPDATE chat_sessions SET unindexed = $v "
        "WHERE user_id = $uid AND project_id = $pid AND meta::id(id) IN $ids",
        {"v": value, "uid": session["user_id"], "pid": session["project_id"],
         "ids": sorted(ids - {session_id})},
    )


async def update_session_command(
    db, session_id: str, session: dict, body: SessionUpdate, user: dict,
) -> dict:
    """Apply a PATCH to an already-gated session row and return it serialized.

    Fields are handled in a fixed order, so the first refused field decides the
    error: reasoning_effort (validated by the route before this runs), then
    agent_auto (clamped, never refused), then the reparent (400 / 403), then
    unindexed (400 on a note). Every refusal lands before the first write.
    """
    sets: list[str] = ["updated_at = time::now()"]
    params: dict = {"id": session_id}
    _llm_config_sets(body, sets, params)
    _ai_only_sets(session, body, sets, params)
    await _agent_flag_sets(session, body, user, sets, params)
    await _reparent_sets(db, session, body, user, sets, params)
    _unindexed_sets(session, body, sets, params)
    if "unindexed" in params:
        await _propagate_unindexed(db, session_id, session, params["unindexed"])
    rows = await db.query(
        f"UPDATE type::record('chat_sessions', $id) SET {', '.join(sets)} RETURN AFTER",
        params,
    )
    if "unindexed" in params:
        await schedule_chat_index(session["user_id"], session["project_id"])
    return serialize_session(rows[0], await build_session_ref_map(db, rows[0]),
                             viewer_id=user["user_id"])
