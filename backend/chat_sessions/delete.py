"""Delete a chat session: the soft-delete cascade and the note realtime nudge."""

import logging

import event_bus
from chat_sessions.note_events import broadcast_doc_id, fire_note_emit

logger = logging.getLogger(__name__)


async def delete_session_command(db, session_id: str, session: dict) -> None:
    """Soft-delete an already-gated session and its messages."""
    await db.query(
        "UPDATE messages SET deleted_at = time::now() "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": session_id},
    )
    await db.query(
        "UPDATE type::record('chat_sessions', $id) SET deleted_at = time::now()",
        {"id": session_id},
    )
    # WHY: realtime nudge for a deleted note. Fire-and-forget (background task) —
    # broadcast_doc_id resolves the parent document against the fetched row, a DB
    # round-trip kept off the request path. The note's anchor lives on its row, so
    # deleting the row is the whole of it: the text is never touched.
    if session.get("is_note") is True:
        fire_note_emit(_emit_note_deleted(db, session_id, session))


async def _emit_note_deleted(db, session_id: str, session: dict) -> None:
    try:
        eid = await broadcast_doc_id(db, session)
        await event_bus.emit(
            "note_session_deleted",
            entity_type="doc", entity_id=eid,
            event={"type": "note_session_deleted", "session_id": session_id},
        )
    except Exception:
        logger.warning("note_session_deleted emit failed", exc_info=True)
