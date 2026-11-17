"""Migration: chat_context_drop — remove chat_sessions.context_document_ids.

# ARCH: a chat carries no pinned document context — the agent finds documents
#   through the project with its own tools. schema.surql carries the same
#   REMOVE for fresh DBs; this migration REMOVEs the field on DBs created while
#   the column lived (`DEFINE FIELD IF NOT EXISTS` never redefines an existing
#   field), so the fingerprint guard reads the field loss as intentional
#   instead of `removed` drift. No code reads or writes the field.

Idempotent (`IF EXISTS`); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_chat_context_drop(db) -> None:
    """Remove chat_sessions.context_document_ids (idempotent)."""
    await db.query("REMOVE FIELD IF EXISTS context_document_ids ON chat_sessions")
    logger.info("chat_context_drop: removed chat_sessions.context_document_ids")
