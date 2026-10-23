"""System note creation (is_note chat_sessions) shared by the pipeline.

# SYSTEM: notes — anchorless system notes pinned to a document (errors and other system messages).

Creates a chat_sessions row with is_note=True plus a single user-role message
holding the note body. This is the single source of truth for system-generated
notes; the extractor pipeline (`_create_error_note`) delegates here so the note
shape (and the PIPELINE_AUTHOR_ID "system notes" author sentinel) can never
drift.

# INVARIANT: never raises. A diagnostic note is best-effort — a failure to record
a failure must never break its caller (mirrors telemetry_store INVARIANT:
diagnostics must be safe to drop). All errors are logged and None is returned.  Why: a diagnostic note is best-effort; raising would break the caller of a failure-handler, so errors are logged and None returned (mirrors telemetry_store).
"""
import logging
from uuid import uuid4

from chat_sessions.delete import delete_session_command

from db import create_record, fetch_one, get_db

logger = logging.getLogger(__name__)


async def _drop_failed_session(session_uid: str) -> None:
    """Delete a just-created system-note session whose first message failed."""
    # WHY: a note exists only together with its first message — a failed write
    # would leave exactly the empty anchorless card the public path guards
    # against (public_notes deletes the session on the same failure), so the
    # just-created session goes with it. Best-effort on purpose: a delete
    # failure falls into the caller's outer arm — still None, still never
    # raises (the module INVARIANT above).
    session = await fetch_one("chat_sessions", session_uid)
    if session:
        db = await get_db()
        await delete_session_command(db, session_uid, session)


async def create_system_note(
    *,
    project_id: str,
    document_id: str,
    title: str,
    body: str,
    author_id: str,
) -> str | None:
    """Create an anchorless system note on a document.

    Returns the new chat_session (note) id, or None on failure (never raises).
    The caller owns the domain-specific event emit (e.g. extraction_error) so
    this helper stays free of event-contract coupling.
    """
    session_uid = str(uuid4())
    try:
        await create_record("chat_sessions", session_uid, {
            "project_id": project_id,
            "user_id": author_id,
            "title": title,
            "model": "",
            "document_id": document_id,
            "is_note": True,
        })
        msg_uid = str(uuid4())
        try:
            await create_record("messages", msg_uid, {
                "chat_id": session_uid,
                "role": "user",
                "content": body,
            })
        except Exception:
            # Re-raised into the outer arm: still None, still never raises.
            await _drop_failed_session(session_uid)
            raise
        db = await get_db()
        await db.query(
            "UPDATE type::record('chat_sessions', $id) SET updated_at = time::now()",
            {"id": session_uid},
        )
        return session_uid
    except Exception as e:
        logger.error("Failed to create system note on doc %s: %s", document_id, e)
        return None


__all__ = ["create_system_note"]
