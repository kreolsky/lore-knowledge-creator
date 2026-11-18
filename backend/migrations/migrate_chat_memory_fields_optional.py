"""Migration: chat_memory_fields_optional — widen chat_sessions.memory.* to option<…>.

# ARCH: stores created before the widening hold memory.message_id / memory.chats /
#   memory.chats[*].sid / memory.chats[*].date as non-option types; schema.surql
#   declares them `IF NOT EXISTS … option<…>`, which never redefines an existing
#   field. This entry OVERWRITEs them, so the fingerprint guard reads the type
#   change as a migration's work instead of `removed` drift. A widening: every
#   stored value satisfies the new type, no row is rewritten.

Idempotent (re-overwriting an option field is a no-op); no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger

_WIDEN = (
    "DEFINE FIELD OVERWRITE memory.message_id ON chat_sessions TYPE option<string>;"
    "DEFINE FIELD OVERWRITE memory.chats ON chat_sessions TYPE option<array<object>>;"
    "DEFINE FIELD OVERWRITE memory.chats[*].sid ON chat_sessions TYPE option<string>;"
    "DEFINE FIELD OVERWRITE memory.chats[*].date ON chat_sessions TYPE option<string>;"
)


async def _migrate_chat_memory_fields_optional(db) -> None:
    """Redefine the four nested memory fields as option<…>.

    query_raw + per-statement status (query() swallows DB errors), so a refused
    statement fails the migration and it retries next boot instead of being
    recorded as applied.
    """
    raw = await db.query_raw(_WIDEN)
    failed = [r for r in raw.get("result") or [] if r.get("status") != "OK"]
    if failed or not raw.get("result"):
        raise RuntimeError(f"chat_memory_fields_optional: redefine failed: {raw}")
    logger.info("chat_memory_fields_optional: memory.* nested fields widened to option<…>")
