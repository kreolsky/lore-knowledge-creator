"""Record CRUD + projected column registries over the DBPool connection.

Split out of the former backend/db.py (behavior-preserving).
"""

from __future__ import annotations

import logging

from surrealdb.errors import SurrealError

from db.pool import get_db
from db.records import extract_id, record_refs, validate_record_id

logger = logging.getLogger("db")


async def create_record(table: str, uid: str, data: dict) -> dict:
    """Create a SurrealDB record with a specific UUID and return the created record dict.

    The surrealdb SDK 1.0.4 does NOT raise Python exceptions on DB errors (e.g. UNIQUE
    constraint violations). Instead it returns the error message as a plain str.
    Success returns a list of dicts; errors return a bare str.

    Raises:
        RuntimeError: If SurrealDB returns an error (str result or missing id).
    """
    # H-2: Validate table name before f-string embedding
    validate_record_id(table)
    db = await get_db()
    # WHY: surrealdb 1.0.4 returned a plain str on a DB error (e.g. record already exists);
    # 2.0.0 RAISES a typed SurrealError instead. Translate it back to this function's
    # documented RuntimeError contract so callers (and `except RuntimeError` guards on the
    # "already exists" path) keep working across the SDK upgrade.
    try:
        rows = await db.query(
            f"CREATE type::record('{table}', $id) CONTENT $data RETURN AFTER",
            {"id": uid, "data": data},
        )
    except SurrealError as e:
        logger.error("CREATE %s:%s failed: %s", table, uid, e)
        raise RuntimeError(f"CREATE {table}:{uid} failed: {e}") from e
    # Defensive: older SDK behavior returned a plain str on error.
    if isinstance(rows, str):
        logger.error("CREATE %s:%s failed: %s", table, uid, rows)
        raise RuntimeError(f"CREATE {table}:{uid} failed: {rows}")
    if not rows:
        logger.error("CREATE %s:%s returned no result", table, uid)
        raise RuntimeError(f"CREATE {table}:{uid} returned no result")
    first = rows[0]
    if not isinstance(first, dict) or "id" not in first:
        logger.error("CREATE %s:%s unexpected result: %s", table, uid, first)
        raise RuntimeError(f"CREATE {table}:{uid} failed")
    return first


async def fetch_one(table: str, uid: str) -> dict | None:
    """Fetch a single record by UUID. Returns None if not found or soft-deleted."""
    # H-2: Validate table name before f-string embedding
    validate_record_id(table)
    db = await get_db()
    rows = await db.query(
        f"SELECT * FROM type::record('{table}', $id)",
        {"id": uid},
    )
    if not rows:
        return None
    record = rows[0]
    # Soft-delete check (works for all tables, even those without deleted_at)
    if record.get("deleted_at") is not None:
        return None
    return record


# ─── Projected document column lists (schema-sync source of truth) ───────────
# Each is a hand-written projected SELECT over the `documents` table. A schema
# rename/add has NO compile-time signal here, so a projection can silently omit a
# field an access check depends on (runtime-only failure). The centralized registry
# below is asserted against surreal/schema.surql's DEFINE FIELD set by
# test_projection_schema_sync — adding/renaming a schema column without updating a
# projection that reads it fails CI. `id` is the implicit record id (no DEFINE FIELD)
# and is the only allowed non-schema column. content + ydoc_state are intentionally
# absent from every metadata-only projection.
DOC_META_COLUMNS: tuple[str, ...] = (
    "id", "project_id", "parent_id", "is_reference", "media_type", "title",
    "file_path", "file_meta", "deleted_at",
)
# Reference panel/editor metadata list (routes/references.py). `has_content` is a
# computed alias (string::len(content ?? '') > 0 AS has_content) added by the route —
# NOT a plain column, so it lives outside this tuple and is exempt from the sync check.
REF_META_COLUMNS: tuple[str, ...] = (
    "id", "project_id", "parent_id", "sort_key", "title", "media_type",
    "source_url", "path", "is_index", "is_reference", "processing_status",
    "file_path", "file_meta", "archived", "created_at", "updated_at",
    "created_by", "created_by_name",
)
# Batch soft-delete access probe (documents.delete).
DOC_BATCH_DELETE_COLUMNS: tuple[str, ...] = (
    "id", "project_id", "parent_id", "is_reference", "is_system", "system_role",
)
# Batch transclusion fetch (documents.batch_read). content IS projected here (transclusion
# needs the body); ydoc_state stays excluded.
DOC_TRANSCLUSION_COLUMNS: tuple[str, ...] = (
    "id", "project_id", "parent_id", "title", "content", "media_type",
    "file_path", "file_meta", "is_reference",
)
_DOC_PROJECTIONS: dict[str, tuple[str, ...]] = {
    "fetch_doc_meta": DOC_META_COLUMNS,
    "references_ref_meta": REF_META_COLUMNS,
    "documents_batch_delete": DOC_BATCH_DELETE_COLUMNS,
    "documents_transclusion": DOC_TRANSCLUSION_COLUMNS,
}


async def fetch_doc_meta(doc_id: str) -> dict | None:
    """Fetch document METADATA only — no ydoc_state, no content.

    # ARCH: access checks and parent/flag reads (get_doc_project_id,
    # get_document_access) only need project_id / is_reference / parent_id, but
    # fetch_one('documents', id) pulls the multi-KB ydoc_state CRDT snapshot AND
    # the full content body on every such check. This projected SELECT omits both,
    # eliminating the largest per-access transfer. Returns None if not found or
    # soft-deleted — same contract as fetch_one.
    """
    validate_record_id(doc_id)
    db = await get_db()
    rows = await db.query(
        f"SELECT {', '.join(DOC_META_COLUMNS)} "
        "FROM type::record('documents', $id)",
        {"id": doc_id},
    )
    if not rows:
        return None
    record = rows[0]
    if record.get("deleted_at") is not None:
        return None
    return record


async def fetch_many(table: str, uids: list[str]) -> dict[str, dict]:
    """Batch-fetch records by UUIDs. Returns {id: record} map, excluding soft-deleted.

    SECURITY: table and UIDs validated inside db.record_refs; ids bind as
    params (the table name is the only value embedded in query text).
    """
    if not uids:
        return {}
    db = await get_db()
    # Build type::record() calls so SurrealDB matches RecordIDs correctly.
    # Plain strings like "notes:uuid" don't match — must use typed records.
    refs, params = record_refs(table, uids)
    rows = await db.query(
        f"SELECT * FROM {table} WHERE id IN [{refs}] AND deleted_at IS NONE",
        params,
    )
    result: dict[str, dict] = {}
    for r in (rows or []):
        rid = extract_id(r.get("id"))
        if rid:
            result[rid] = r
    return result


async def soft_delete(table: str, uid: str) -> None:
    """Mark a record as deleted by setting deleted_at timestamp."""
    # H-2: Validate table name before f-string embedding
    validate_record_id(table)
    db = await get_db()
    await db.query(
        f"UPDATE type::record('{table}', $id) SET deleted_at = time::now()",
        {"id": uid},
    )
