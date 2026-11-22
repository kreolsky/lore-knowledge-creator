"""Update a chat session: one UPDATE built field by field, with field-level access."""

import logging

from fastapi import HTTPException

from access import get_document_access, get_project_access
from chat_sessions.conversation import conversation_session_ids
from chat_sessions.recall import (
    mark_topics_first,
    schedule_chat_index,
    schedule_chat_topics,
)
from chat_sessions.serialize import (
    build_session_ref_map,
    serialize_session,
)
from db import fetch_doc_meta
from event_bus import emit
from jobs.tasks.chat_topics import mark_persona_memory
from models import SessionUpdate

logger = logging.getLogger(__name__)


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
    """reasoning_effort and the thread's active-branch pointer;
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
    """agent_auto (the confirm↔auto selector)."""
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


# ARCH: reparent = re-anchor. The write is
# chat_sessions.document_id — the column the proximity sort and build_ref_map
# read; there is no separate "chat context doc" field. A reference-scoped
# session holds the REFERENCE id in that column, so moving it to a document is
# the same write and the row becomes a plain document chat (reference_id is a
# read-only derivation). Refused (400) for note sessions — a note is anchored to
# its owning document's text, moving the row would strand the anchor. A non-null
# target passes `check_reparent_target` — the chat's own project, never a
# reference, at least commentator access: reparenting into a document the user
# cannot see would hide their own chat from them and leak a title into that
# branch's proximity ranking. The agent's
# target_doc_id pin is NOT moved: the turn guard only trusts a target that is a
# descendant of the (new) document_id, so a stale pin is inert outside the new
# scope.
def refuse_note_reparent(session: dict) -> None:
    """400 on a note session — it is anchored to its owning document's text.

    Shared by the PATCH path and the agent's `move_chat`, so the refusal lands
    before any write and before the mid-turn hold: a call that can only fail
    must not raise an approval card."""
    if session.get("is_note") is True:
        raise HTTPException(status_code=400, detail="Cannot change parent of a note session")


async def check_reparent_target(
    session: dict, document_id: str, user: dict,
) -> None:
    """The TARGET half of a reparent: a uniform 404 unless `document_id` is a
    live document of the chat's own project, 400 on a reference, 403 below
    commentator access. Raises before any write."""
    # INVARIANT(security): a chat moves only within its own project.
    # Why: operator ruling — access alone resolved the TARGET's project and
    # never compared it with the chat's, so a member of two projects could park
    # a chat of one on a document of the other, out of reach of the chat list
    # and the recall scope it belongs to.
    target = await fetch_doc_meta(document_id)
    if not target or target.get("project_id") != session["project_id"]:
        # Missing, deleted and cross-project share ONE answer — no existence
        # oracle (the scope.gate_mutation_target convention).
        raise HTTPException(status_code=404, detail="Target document not found")
    # INVARIANT: a chat can be moved to any document, never onto a reference.
    # Why: operator ruling — "чат можно передать на любой документ, на референс не надо".
    if target.get("is_reference"):
        raise HTTPException(status_code=400, detail="Cannot move a chat onto a reference")
    target_access = await get_document_access(document_id, user)
    if target_access is None or target_access == "readonly":
        raise HTTPException(
            status_code=403, detail="Commentator access required on the target document",
        )


async def _reparent_sets(
    db, session: dict, body: SessionUpdate, user: dict, sets: list[str], params: dict,
) -> None:
    if "document_id" not in body.model_fields_set:
        return
    refuse_note_reparent(session)
    if body.document_id is None:
        sets.append("document_id = NONE")
        return
    await check_reparent_target(session, body.document_id, user)
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


async def _write_conversation(db, session_id: str, session: dict, assignment: str, value) -> set[str]:
    """Apply `assignment` (binding `$v`) to every row of the conversation, the
    addressed one included, then to the rows that appeared meanwhile; returns
    the ids written.

    WHY two walks: a branch or continuation created while this PATCH runs is
    missing from the first walk; one created before the second walk is in it,
    and one created after re-copies from a source the first pass already wrote
    (see the INVARIANT in chat_sessions.conversation.inherit_conversation_fields).
    """
    written: set[str] = set()
    for _ in range(2):
        ids = await conversation_session_ids(db, session_id, session) - written
        if not ids:
            return written
        await db.query(
            f"UPDATE chat_sessions SET {assignment} "
            "WHERE user_id = $uid AND project_id = $pid AND meta::id(id) IN $ids",
            {"v": value, "uid": session["user_id"], "pid": session["project_id"],
             "ids": sorted(ids)},
        )
        written |= ids
    return written


async def _propagate_unindexed(db, session_id: str, session: dict, value: bool) -> None:
    """Write the flag to every row of the conversation."""
    # INVARIANT(security): the flag is one value per conversation — a toggle on
    # one branch writes every branch and compaction continuation.
    # Why: a sibling left unflagged keeps the same transcript searchable.
    await _write_conversation(db, session_id, session, "unindexed = $v", value)


async def _bind_origin_persona(db, session_id: str, session: dict, value: str) -> None:
    """Write the persona binding to every row of the conversation, then post the
    chat-topics reconcile with those rows taken first (every thread's newest
    branch is among them)."""
    # INVARIANT(persisted): the binding is one value per conversation — every
    # branch and compaction continuation — and its topics are KEPT as they are.
    # Why: operator ruling — a branch or continuation must not stay in the old
    # persona's memory; topics derive from the chat's text, not from the persona,
    # so a Default binding only stops labelling and showing them.
    written = await _write_conversation(db, session_id, session, "origin_persona_id = $v", value)
    await mark_topics_first(session["user_id"], session["project_id"], written)
    await schedule_chat_topics(session["user_id"], session["project_id"])


async def _propagate_parent(
    db, session_id: str, session: dict, value: str | None,
) -> set[str]:
    """Write the new parent to every row of the conversation (mirrors
    `_propagate_unindexed`) and return the ids written. `value=None` clears the
    parent on every row alike."""
    # INVARIANT: a reparent writes EVERY branch and compaction continuation,
    # not only the addressed row.
    # Why: recall_search.root_documents treats the thread as ONE scope, and a
    # chat-sourced fact follows its row's `document_id` in the retrieval
    # narrow — a sibling left on the old parent splits the conversation's
    # facts between two narrows.
    # WHY: a clear writes the literal NONE (field absent), matching the
    # addressed row's `document_id = NONE` — a bound None would store NULL.
    assignment = "document_id = $v" if value is not None else "document_id = NONE"
    return await _write_conversation(db, session_id, session, assignment, value)


async def _push_session_parent(
    project_id: str, session_id: str, out: dict, written: set[str],
) -> None:
    """Tell the user's OTHER sockets the chat's new parent — a `session_parent`
    chat frame over the user-filtered chat channel (bus event `chat_frame_push`
    → routes.chat.fanout, the detached image run's route). They hold no PATCH
    response to read, so without the frame their chat list keeps the old parent
    until a reload. Best-effort: a failed push logs and the write stands (a
    reload reads the row)."""
    try:
        await emit(
            "chat_frame_push",
            project_id=project_id,
            session_id=session_id,
            frame={"type": "session_parent", "session": out, "session_ids": sorted(written)},
        )
    except Exception:
        logger.warning(
            "chat reparent: frame push failed session=%s", session_id, exc_info=True,
        )


async def update_session_command(
    db, session_id: str, session: dict, body: SessionUpdate, user: dict,
) -> dict:
    """Apply a PATCH to an already-gated session row and return it serialized.

    Fields are handled in a fixed order, so the first refused field decides the
    error: reasoning_effort (validated by the route before this runs), then
    agent_auto (clamped, never refused), then the reparent (400 on a note or a
    reference target, 404 on a target of another project, 403 below
    commentator), then unindexed (400 on a note). Every refusal lands before
    the first write.
    """
    sets: list[str] = ["updated_at = time::now()"]
    params: dict = {"id": session_id}
    moved: set[str] | None = None
    _llm_config_sets(body, sets, params)
    _ai_only_sets(session, body, sets, params)
    await _agent_flag_sets(session, body, user, sets, params)
    await _reparent_sets(db, session, body, user, sets, params)
    # WHY: keyed on the request field, not on `params` — the clear arm of
    # `_reparent_sets` writes a literal `NONE` and never fills `params`, and a
    # clear must reach every branch exactly like a move.
    if "document_id" in body.model_fields_set:
        moved = await _propagate_parent(db, session_id, session, body.document_id)
    _unindexed_sets(session, body, sets, params)
    # INVARIANT(persisted): the persona selected for a chat IS its memory binding.
    # Why: operator ruling — persona and memory are one choice.
    # A persona PATCH (set or cleared) re-binds the whole conversation; a cleared
    # persona = '' = Default, in no persona's memory.
    if "system_prompt_id" in body.model_fields_set and session.get("is_note") is not True:
        await _bind_origin_persona(db, session_id, session, body.system_prompt_id or "")
    if "unindexed" in params:
        await _propagate_unindexed(db, session_id, session, params["unindexed"])
    rows = await db.query(
        f"UPDATE type::record('chat_sessions', $id) SET {', '.join(sets)} RETURN AFTER",
        params,
    )
    if "unindexed" in params:
        await schedule_chat_index(session["user_id"], session["project_id"])
    out = serialize_session(rows[0], await build_session_ref_map(db, rows[0]),
                            viewer_id=user["user_id"])
    if session.get("is_note") is not True:
        await mark_persona_memory(db, session["project_id"], [out])
    # ARCH: the reparent's live update rides the COMMAND, so the human PATCH
    # and the agent's move_chat push the user's sockets the same frame.
    if moved is not None:
        await _push_session_parent(session["project_id"], session_id, out, moved)
    return out
