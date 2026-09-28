"""System note creation (is_note chat_sessions) shared by pipeline + agent.

# SYSTEM: notes — anchorless system notes pinned to a document (errors and other system messages).

Creates a chat_sessions row with is_note=True plus a single user-role message
holding the note body. This is the single source of truth for system-generated
notes; both the extractor pipeline (`_create_error_note`) and the agent proposal
apply path (`_create_agent_error_note`) delegate here so the note shape (and the
PIPELINE_AUTHOR_ID "system notes" author sentinel) can never drift.

# INVARIANT: never raises. A diagnostic note is best-effort — a failure to record
a failure must never break its caller (mirrors telemetry_store INVARIANT:
diagnostics must be safe to drop). All errors are logged and None is returned.  Why: a diagnostic note is best-effort; raising would break the caller of a failure-handler, so errors are logged and None returned (mirrors telemetry_store).
"""
import logging
from uuid import uuid4

from db import create_record, get_db

logger = logging.getLogger(__name__)


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
    The caller owns the domain-specific event emit (e.g. extraction_error /
    agent_error_note) so this helper stays free of event-contract coupling.
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
        await create_record("messages", msg_uid, {
            "chat_id": session_uid,
            "role": "user",
            "content": body,
        })
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
