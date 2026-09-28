"""Migration: document_access_drop — per-document access overrides removed.

# ARCH: a document inherits its project's access level (access.get_document_access),
#   so the `document_access` table and per-document pending invites have no reader.
#   schema.surql no longer defines either; this migration drops them on DBs created
#   before the drop. The counts are logged BEFORE anything is removed so the operator
#   can see how many overrides (and whose raised access) went away. schema.surql does
#   not carry the REMOVE itself: apply_schema runs before migrations and would drop
#   the rows before they are counted.

Idempotent; a no-op on a fresh DB.
"""

from __future__ import annotations

from db import extract_id
from migrations._shared import logger


async def _table_exists(db, name: str) -> bool:
    info = await db.query("INFO FOR DB")
    blob = info[0] if isinstance(info, list) else info
    return name in ((blob or {}).get("tables") or {})


async def _count(db, sql: str) -> int:
    rows = await db.query(sql)
    return rows[0]["n"] if rows else 0


async def _dedup_project_invites(db) -> int:
    """Keep the newest pending invite per (email, project_id); returns rows deleted.

    # WHY: the unique index narrows from (email, project_id, document_id) to
    # (email, project_id); a pre-existing duplicate would make the new index fail.
    """
    rows = await db.query(
        "SELECT id, email, project_id, created_at FROM pending_invites ORDER BY created_at DESC",
    )
    seen: set[tuple[str, str]] = set()
    stale: list[str] = []
    for r in rows or []:
        key = (r["email"], r["project_id"])
        if key in seen:
            stale.append(extract_id(r["id"]))
        else:
            seen.add(key)
    for pid in stale:
        await db.query("DELETE type::record('pending_invites', $id)", {"id": pid})
    return len(stale)


async def _migrate_document_access_drop(db) -> None:
    """Drop document_access and per-document pending invites (idempotent)."""
    has_table = await _table_exists(db, "document_access")
    overrides = (
        await _count(db, "SELECT count() AS n FROM document_access GROUP ALL")
        if has_table else 0
    )
    doc_invites = await _count(
        db,
        "SELECT count() AS n FROM pending_invites WHERE document_id IS NOT NONE GROUP ALL",
    )
    logger.info(
        "document_access_drop: %d document_access row(s), %d per-document pending invite(s)",
        overrides, doc_invites,
    )
    await db.query("DELETE pending_invites WHERE document_id IS NOT NONE")
    deduped = await _dedup_project_invites(db)
    logger.info("document_access_drop: %d duplicate project invite(s) deleted", deduped)
    await db.query("REMOVE INDEX IF EXISTS idx_pi_unique ON pending_invites")
    await db.query("REMOVE FIELD IF EXISTS document_id ON pending_invites")
    await db.query(
        "DEFINE INDEX OVERWRITE idx_pi_unique ON pending_invites FIELDS email, project_id UNIQUE",
    )
    await db.query("REMOVE TABLE IF EXISTS document_access")
