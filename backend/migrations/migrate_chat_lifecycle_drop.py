"""Migration: chat_lifecycle_drop — remove chat_sessions.lifecycle.

# ARCH: the driver owns every turn, so there is no transport to pick and no
#   column to hold the pick. schema.surql carries the same REMOVE for fresh
#   DBs; this migration REMOVEs the field on DBs created while the column
#   lived (`DEFINE FIELD IF NOT EXISTS` never redefines an existing field),
#   so the fingerprint guard reads the field loss as intentional instead of
#   `removed` drift. No code reads or writes the field.

Idempotent (`IF EXISTS`); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_chat_lifecycle_drop(db) -> None:
    """Remove chat_sessions.lifecycle (idempotent)."""
    await db.query("REMOVE FIELD IF EXISTS lifecycle ON chat_sessions")
    logger.info("chat_lifecycle_drop: removed chat_sessions.lifecycle")
