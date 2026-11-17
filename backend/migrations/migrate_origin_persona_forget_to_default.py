"""Migration: origin_persona_forget_to_default — the «не запоминать» binding becomes Default.

`origin_persona_id = 'forget'` kept a chat out of every persona's memory.
Default ('') does the same — only persona documents turn memory on — so the
sentinel goes and its rows are re-bound to ''.

Idempotent: only 'forget' rows are written; no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_origin_persona_forget_to_default(db) -> None:
    """Re-bind origin_persona_id 'forget' → ''."""
    rows = await db.query(
        "UPDATE chat_sessions SET origin_persona_id = '' "
        "WHERE origin_persona_id = 'forget' RETURN id",
    )
    logger.info("origin_persona_forget_to_default: %d chat(s) re-bound", len(rows or []))
