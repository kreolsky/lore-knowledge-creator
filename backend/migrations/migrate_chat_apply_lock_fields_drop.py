"""Migration: chat_apply_lock_fields_drop — remove the chat branch lock's fields.

# ARCH (plan remove-chat-branch-lock): has_applied_edit and
#   frozen_anchor_message_id are deleted with the apply-lock — a chat is never
#   frozen; editing a message and forking from it stay available for the life
#   of a session, including after a tool has applied a document mutation.
#   schema.surql no longer DEFINES the fields (it carries the same REMOVEs for
#   fresh DBs); this migration REMOVEs them on DBs created before the deletion
#   (`DEFINE FIELD IF NOT EXISTS` never redefines an existing field), so the
#   fingerprint guard reads the field loss as intentional instead of `removed`
#   drift. No code reads or writes either field anymore; the values on live
#   rows are lock state for a lock that no longer exists.

Idempotent (`IF EXISTS`); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_chat_apply_lock_fields_drop(db) -> None:
    """Remove the retired apply-lock fields from chat_sessions (idempotent)."""
    await db.query("REMOVE FIELD IF EXISTS has_applied_edit ON chat_sessions")
    await db.query("REMOVE FIELD IF EXISTS frozen_anchor_message_id ON chat_sessions")
    logger.info("chat_apply_lock_fields_drop: removed the apply-lock fields")
