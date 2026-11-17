"""Migration: origin_persona_follow_persona — re-align every chat's memory binding to its persona.

The persona selected for a chat IS its memory binding (`origin_persona_id`
follows every system_prompt_id PATCH — chat_sessions/update.py). Chats bound
under the old write-once rule may sit in a persona's memory they no longer use;
this entry re-binds each conversation to the persona of its most recently
updated row (`system_prompt_id`, NONE ⇒ '' = Default). Accepted by the operator:
an old chat may move to another persona's memory or out of memory entirely.
Topics reconcile on the chat-topics job's next run, not here.

Idempotent: only rows whose binding differs are written; no-op on a fresh DB.
"""

from __future__ import annotations

from migrations._shared import logger


def _thread_personas(rows: list[dict]) -> dict[str, str]:
    """thread → the system_prompt_id ?? '' of its most recently updated row."""
    newest: dict[str, dict] = {}
    for r in rows:
        cur = newest.get(r["thread"])
        if cur is None or r["updated_at"] > cur["updated_at"]:
            newest[r["thread"]] = r
    return {t: r.get("system_prompt_id") or "" for t, r in newest.items()}


async def _migrate_origin_persona_follow_persona(db) -> None:
    """Set origin_persona_id on every live non-note chat to its thread's current persona."""
    rows = await db.query(
        "SELECT meta::id(id) AS sid, thread_id ?? meta::id(id) AS thread, "
        "system_prompt_id, origin_persona_id, updated_at FROM chat_sessions "
        "WHERE is_note = false AND deleted_at IS NONE",
    ) or []
    personas = _thread_personas(rows)
    stale: dict[str, list[str]] = {}
    for r in rows:
        want = personas[r["thread"]]
        if r.get("origin_persona_id") != want:
            stale.setdefault(want, []).append(r["sid"])
    for persona, sids in stale.items():
        await db.query(
            "UPDATE chat_sessions SET origin_persona_id = $p WHERE meta::id(id) IN $ids",
            {"p": persona, "ids": sids},
        )
    logger.info("origin_persona_follow_persona: %d chat(s) re-bound",
                sum(len(s) for s in stale.values()))
