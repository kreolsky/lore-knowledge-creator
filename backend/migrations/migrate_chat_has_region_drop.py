"""Migration: chat_has_region_drop — remove chat_sessions.has_region.

# ARCH: the agent is never confined to a text span, so there is no pin flag to
#   hold. schema.surql carries the same REMOVE for fresh DBs; this migration
#   REMOVEs the field on DBs created while the column lived (`DEFINE FIELD IF
#   NOT EXISTS` never redefines an existing field), so the fingerprint guard
#   reads the field loss as intentional instead of `removed` drift. Former
#   pinned sessions become ordinary chats. No code reads or writes the field.

Idempotent (`IF EXISTS`); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_chat_has_region_drop(db) -> None:
    """Remove chat_sessions.has_region (idempotent)."""
    await db.query("REMOVE FIELD IF EXISTS has_region ON chat_sessions")
    logger.info("chat_has_region_drop: removed chat_sessions.has_region")
