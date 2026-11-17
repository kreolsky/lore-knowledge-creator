"""Migration: origin_persona_backfill — stamp chat_sessions.origin_persona_id on pre-field rows.

A chat belongs to the persona it started with (`origin_persona_id`, stamped
write-once at a chat's first turn). Rows older than the field carry NONE; this
entry stamps every non-note one with its CURRENT persona (`system_prompt_id`,
NONE ⇒ '' = Default). The misattribution of an old chat whose persona was
switched mid-way is accepted by the operator — its start persona was never
stored.

Idempotent: only NONE rows are written; no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_origin_persona_backfill(db) -> None:
    """Stamp origin_persona_id = system_prompt_id ?? '' on unstamped non-note chats."""
    rows = await db.query(
        "UPDATE chat_sessions SET origin_persona_id = system_prompt_id ?? '' "
        "WHERE origin_persona_id IS NONE AND is_note = false RETURN id",
    )
    logger.info("origin_persona_backfill: %d chat(s) stamped", len(rows or []))
