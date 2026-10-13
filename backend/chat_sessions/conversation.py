"""The conversation a chat session belongs to: every branch and compaction continuation."""
from db import extract_id

# A conversation never grows this deep; the cap only bounds a corrupt cycle.
_MAX_HOPS = 50


async def conversation_session_ids(db, session_id: str, session: dict) -> set[str]:
    """Ids of every chat_sessions row in `session`'s conversation, itself included.

    The closure over two links, in both directions: `thread_id` (branches share
    the root's id; a pre-migration root has thread_id NONE and IS its own
    thread) and `compacted_from` (a continuation carries no thread_id, and a
    branch of a continuation threads on the continuation's id). Structural:
    deleted rows are walked too, so a dead link never splits the conversation.
    Only the session owner's rows in the session's project are ever returned.
    """
    members = {session_id}
    found: set[str] = set()
    for _ in range(_MAX_HOPS):
        rows = await db.query(
            "SELECT meta::id(id) AS sid, thread_id, compacted_from FROM chat_sessions "
            "WHERE user_id = $uid AND project_id = $pid "
            "AND (meta::id(id) IN $ids OR thread_id IN $ids OR compacted_from IN $ids)",
            {"uid": session["user_id"], "pid": session["project_id"],
             "ids": sorted(members)},
        ) or []
        found = {r["sid"] for r in rows}
        links = {r[k] for r in rows for k in ("thread_id", "compacted_from") if r.get(k)}
        grown = members | found | links
        if grown == members:
            break
        members = grown
    return found | {session_id}


def origin_of(row: dict) -> str:
    """The row's origin — its source row's identity across every copy."""
    return row.get("origin_id") or str(extract_id(row.get("id")) or "")
