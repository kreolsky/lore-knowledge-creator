"""The conversation a chat session belongs to: every branch and compaction continuation."""
from datetime import datetime, timezone

from db import extract_id, parse_iso

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


def _utc(value: object) -> datetime | None:
    if isinstance(value, str):
        value = parse_iso(value)
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def last_message_at(db, ids: list[str]) -> dict[str, datetime]:
    """Newest live (non-deleted) message's `created_at` per chat id, over
    exactly `ids`, in one grouped query; a chat with no live message is absent."""
    if not ids:
        return {}
    rows = await db.query(
        "SELECT chat_id, time::max(created_at) AS last_at FROM messages "
        "WHERE chat_id IN $ids AND deleted_at IS NONE GROUP BY chat_id",
        {"ids": sorted(set(ids))},
    ) or []
    return {r["chat_id"]: t for r in rows if (t := _utc(r.get("last_at")))}


def newest_branch_per_thread(rows: list[dict], last_at: dict[str, datetime]) -> list[dict]:
    """Per thread (`root`), the row whose newest message is the latest; on a
    tie the later-created branch. A row absent from `last_at` is never chosen.

    Rows carry `sid`, `root` (= thread_id or sid) and `created_at`. The
    chat-topics labeller and the persona memory block choose a thread's branch
    here and nowhere else — operator ruling: memory shows the branch with the
    newest leaf, and two rules would let the block show a branch whose toc
    nobody verifies; a tie goes to the newer branch, created to be worked on.
    """
    floor = datetime.min.replace(tzinfo=timezone.utc)
    best: dict[str, dict] = {}
    for row in rows:
        if row["sid"] not in last_at:
            continue
        key = (last_at[row["sid"]], _utc(row.get("created_at")) or floor)
        held = best.get(row["root"])
        if held is None or key > (last_at[held["sid"]], _utc(held.get("created_at")) or floor):
            best[row["root"]] = row
    return list(best.values())


def origin_of(row: dict) -> str:
    """The row's origin — its source row's identity across every copy."""
    return row.get("origin_id") or str(extract_id(row.get("id")) or "")


async def inherit_conversation_fields(db, new_id: str, source_id: str) -> dict:
    """Re-copy the conversation-wide fields onto a freshly created row from its
    source, in one statement; returns the row after.

    INVARIANT(security): a new branch / compaction continuation ends on the
    conversation's CURRENT unindexed / origin_persona_id / document_id, not the
    values its creator read before the create. Why: a PATCH that walked the
    conversation before the row existed never writes it, so a stale copy would
    make an anonymous chat's continuation searchable or keep it in the old
    persona's memory. The PATCH half re-walks after writing the source
    (chat_sessions.update._write_conversation); between the two halves every
    interleaving ends on the PATCHed value.
    """
    rows = await db.query(
        "UPDATE type::record('chat_sessions', $new) SET "
        "unindexed = type::record('chat_sessions', $src).unindexed ?? false, "
        "origin_persona_id = type::record('chat_sessions', $src).origin_persona_id, "
        "document_id = type::record('chat_sessions', $src).document_id "
        "RETURN AFTER",
        {"new": new_id, "src": source_id},
    ) or []
    return rows[0] if rows else {}
