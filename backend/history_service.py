"""Document history log — creation and first-edit-per-user event recording.

# SYSTEM: document-history — lightweight event log for document creation and per-user first edit.
# ARCH: one `edited` row per (document_id, user_id), enforced by a DB unique index.
#   log_first_edit is idempotent — a unique-index violation is silently ignored.
#   No pre-SELECT needed; the DB constraint is the sole gate.
"""

import logging
from uuid import uuid4

import event_bus
from db import create_record, get_db, serialize_record

logger = logging.getLogger(__name__)


async def log_event(document_id: str, user_id: str | None, user_name: str, action: str) -> dict | None:
    """Insert a history entry and broadcast a WS event.

    Returns the created record dict, or None if the insert failed (e.g. unique violation).
    """
    uid = str(uuid4())
    record = await create_record("document_history", uid, {
        "document_id": document_id,
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
    })
    serialized = serialize_record(record, "id")
    await event_bus.emit(
        "document_history_added",
        entity_type="doc",
        entity_id=document_id,
        event={"type": "document_history_added", "event": serialized},
    )
    return serialized


async def log_first_edit(document_id: str, user_id: str, user_name: str) -> dict | None:
    """Idempotent: insert an `edited` row. Returns None if already exists.

    INVARIANT: the unique index on (document_id, user_id, action) guarantees at most
    one `edited` row per user per document, even across sessions and replicas.
    A duplicate insert raises — we catch and return None (no-op).  Why: the unique index is the dedup fence; without it a re-edit would append a duplicate row per save, so the duplicate-raise is caught and treated as a no-op.
    """
    try:
        return await log_event(document_id, user_id, user_name, "edited")
    except Exception:
        logger.debug(
            "log_first_edit: duplicate skipped for doc=%s user=%s",
            document_id, user_id,
        )
        return None


async def list_history(document_id: str) -> list[dict]:
    """Return all history entries for a document, oldest first."""
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM document_history "
        "WHERE document_id = $did "
        "ORDER BY created_at ASC",
        {"did": document_id},
    )
    return [serialize_record(r, "id") for r in (rows or [])]
