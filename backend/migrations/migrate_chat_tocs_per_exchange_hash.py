"""Migration: chat_tocs_per_exchange_hash — labelled_hash → aligned labelled_hashes.

A toc used to carry ONE hash over all its labelled exchanges, so any edit
dropped the whole row; the labeller now stores one content-only hash per
labelled exchange (jobs.tasks.chat_topics.exchange_hash) and drops only the
topics covering a changed one. Per row: the old aggregate still matches the
chat's current exchanges ⇒ labelled_hashes written; else the row is deleted —
what verify_tocs would have done with it. Then the old field is REMOVEd here,
not in schema.surql: schema runs before migrations, and the field must still
be readable for the check (fresh DBs never had it).

Idempotent: only rows with labelled_hashes NONE are read; no-op on a fresh DB.
"""

from __future__ import annotations

from content_hash import hash_content

from jobs.tasks.chat_topics import group_exchanges
from migrations._shared import logger

_ROWS_SQL = (
    "SELECT meta::id(id) AS id, role, model, created_at, "
    "crypto::sha256(content) AS h, string::trim(content) != '' AS has "
    "FROM messages WHERE chat_id = $cid AND deleted_at IS NONE"
)


def _old_aggregate(exchanges: list[dict], ids: list[str]) -> str | None:
    """The retired labelled_hash over `ids`; None when an id vanished."""
    by_id = {e["id"]: e for e in exchanges}
    if any(i not in by_id for i in ids):
        return None
    return hash_content("\n".join(
        "\x1f".join([i, by_id[i]["user"]["h"], *(a["h"] for a in by_id[i]["answers"])])
        for i in ids))


async def _migrate_chat_tocs_per_exchange_hash(db) -> None:
    """Convert or delete every unconverted toc row, then REMOVE labelled_hash."""
    rows = await db.query(
        "SELECT meta::id(id) AS cid, labelled_ids, labelled_hash FROM chat_tocs "
        "WHERE labelled_hashes IS NONE",
    ) or []
    kept = dropped = 0
    for row in rows:
        exchanges, _ = group_exchanges(await db.query(_ROWS_SQL, {"cid": row["cid"]}) or [])
        ids = row.get("labelled_ids") or []
        if row.get("labelled_hash") and _old_aggregate(exchanges, ids) == row["labelled_hash"]:
            hashes = {e["id"]: e["hash"] for e in exchanges}
            await db.query("UPDATE type::record('chat_tocs', $cid) SET labelled_hashes = $h",
                           {"cid": row["cid"], "h": [hashes[i] for i in ids]})
            kept += 1
        else:
            await db.query("DELETE type::record('chat_tocs', $cid)", {"cid": row["cid"]})
            dropped += 1
    await db.query("REMOVE FIELD IF EXISTS labelled_hash ON chat_tocs")
    await db.query("UPDATE chat_tocs UNSET labelled_hash")
    logger.info("chat_tocs_per_exchange_hash: %d toc(s) converted, %d stale dropped", kept, dropped)
