"""Delete a chat session: the soft-delete cascade and the note-link strip."""

import logging
import re
from functools import lru_cache

from agent.doc_state import resolve_live_doc_state, route_document_content

import event_bus
from chat_sessions.note_events import broadcast_doc_id, fire_note_emit

logger = logging.getLogger(__name__)


# ARCH: backend is the single source of truth for
# stripping a [label](note:<id>) anchor link from the owning entity's content on
# note delete. The client-side remove-note-link path was editor-bound (primary
# editor only) and racy with concurrent CRDT edits; routing the strip through the
# CRDT convergence path here is editor-independent and idempotent.
@lru_cache(maxsize=4096)
def _note_link_pattern(note_id: str) -> re.Pattern:
    """[label](note:<id>) → label. Replaces ALL occurrences (re.sub is global).

    NOTE: uses [^\\]]* (zero-or-more) not [^\\]]+ like the legacy client handler —
    intentionally also strips empty-label [](note:<id>) links.
    """
    return re.compile(r"\[([^\]]*)\]\(note:" + re.escape(note_id) + r"\)")


async def delete_session_command(db, session_id: str, session: dict) -> None:
    """Soft-delete an already-gated session and its messages; strip a note's link."""
    await db.query(
        "UPDATE messages SET deleted_at = time::now() "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": session_id},
    )
    await db.query(
        "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
        {"id": session_id},
    )
    # WHY: realtime nudge for a deleted note.
    # Fire-and-forget (background task). Broadcast entity_id is the resolved parent
    # document — resolved against the fetched row.
    #
    # WHY: the SAME background task also strips the
    # [label](note:<id>) anchor link from the owning entity's content BEFORE the
    # realtime emit. Why folded together: (a) one tracked task instead of two;
    # (b) ordering — strip before the note_session_deleted emit so receivers that
    # reconcile on next loadSessions see the stripped content on the same cycle;
    # (c) the no-session convergence branch runs load_ydoc + set_content(persist)
    # + rebuild_doc_mentions + 2 emits — exactly the round-trips the WHY on
    # chat_sessions.note_events._emit_tasks schedules off the request path via
    # fire_note_emit. Best-effort:
    # the note is already soft-deleted, so a strip failure is logged and never
    # blocks the completed DELETE (a leftover link is inert + recoverable on next
    # load). Mutation target is session.document_id (the reference's OWN content,
    # where the link lives) — NOT broadcast_doc_id's parent, which is realtime-
    # routing only.
    if session.get("is_note") is True:
        fire_note_emit(_emit_note_deleted(db, session_id, session))


async def _strip_note_link(session_id: str, session: dict) -> None:
    doc_id = session.get("document_id")
    if not doc_id:
        return
    try:
        current, _tables = await resolve_live_doc_state(doc_id)
        new = _note_link_pattern(session_id).sub(r"\1", current)
        if new != current:
            await route_document_content(
                doc_id=doc_id, new_content=new,
                project_id=session.get("project_id"),
            )
    except Exception:
        logger.warning(
            "note-link strip failed for session %s", session_id, exc_info=True,
        )


async def _emit_note_deleted(db, session_id: str, session: dict) -> None:
    try:
        await _strip_note_link(session_id, session)
        eid = await broadcast_doc_id(db, session)
        await event_bus.emit(
            "note_session_deleted",
            entity_type="doc", entity_id=eid,
            event={"type": "note_session_deleted", "session_id": session_id},
        )
    except Exception:
        logger.warning("note_session_deleted emit failed", exc_info=True)
