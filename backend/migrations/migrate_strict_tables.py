"""Migration: strict_tables — drop undeclared row fields, then define nine tables SCHEMAFULL.

# ARCH: installations older than schema.surql's SCHEMAFULL lines still hold these
#   tables as `TYPE ANY SCHEMALESS` (or `TYPE ANY SCHEMAFULL`), and boot only
#   reports that drift (INVARIANT(data-loss) above `_REF_DB` in db_schema.py). This
#   migration is the one place that rewrites them, once, behind the pre-deploy dump.
# INVARIANT(data-loss): no table is flipped SCHEMAFULL while any row carries an undeclared key.
# Why: the server accepts SCHEMAFULL over rows carrying undeclared fields, and every
#   read of such a row then fails — v0.36.4 made projects unopenable exactly so.
#   The scan covers top-level and nested keys (FLEXIBLE objects excepted); on a hit
#   nothing is flipped and the migration defers.
#
# The UNSET fields are row data nobody reads (operator ruling: fields nobody needs
# are deleted); the pre-deploy dump is their only copy afterwards.

Idempotent: the UNSETs touch only rows still carrying a field; the table
definitions match schema.surql's lines, so a re-run renders the same.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from migrations._shared import MigrationDeferred, logger

_TABLES = (
    "agent_configs", "api_keys", "chat_sessions", "checkpoints", "project_members",
    "projects", "users", "documents", "user_preferences",
)
_DEAD_FIELDS: dict[str, tuple[str, ...]] = {
    "chat_sessions": (
        "anchor_offset_end", "anchor_offset_start", "archived", "archived_from",
        "context_document_ids", "context_reference_ids", "frozen_anchor_message_id",
        "has_applied_edit", "has_region", "reference_id", "system_prompts_doc_id",
    ),
    "projects": ("system_prompts_doc_id",),
    "api_keys": ("key_kind",),
    "users": ("salt",),
}
_SCAN_BATCH = 200


def _first(result: Any) -> dict:
    return result if isinstance(result, dict) else result[0]


async def _unset_dead_fields(db) -> None:
    for table, fields in _DEAD_FIELDS.items():
        present = " OR ".join(f"{f} IS NOT NONE" for f in fields)
        await db.query(f"UPDATE {table} UNSET {', '.join(fields)} WHERE {present}")


def _walk(value: Any, path: str, declared: set[str], flexible: set[str],
          out: Counter) -> None:
    """Count every key path under `value` that `declared` does not name."""
    if path in flexible:
        return
    if isinstance(value, list):
        for item in value:
            _walk(item, f"{path}.*", declared, flexible, out)
        return
    if not isinstance(value, dict):
        return
    for key, child in value.items():
        if not path and key == "id":
            continue
        child_path = f"{path}.{key}" if path else key
        if child_path not in declared:
            out[child_path] += 1
            continue
        _walk(child, child_path, declared, flexible, out)


async def _undeclared_keys(db, table: str) -> Counter:
    """Scan every row of `table` for keys its live field definitions do not declare."""
    fields = _first(await db.query(f"INFO FOR TABLE {table}")).get("fields") or {}
    declared = set(fields)
    flexible = {name for name, d in fields.items() if " FLEXIBLE" in str(d)}
    out: Counter = Counter()
    start = 0
    while True:
        rows = await db.query(
            f"SELECT * FROM {table} START $start LIMIT $limit",
            {"start": start, "limit": _SCAN_BATCH},
        )
        if not rows:
            return out
        for row in rows:
            _walk(row, "", declared, flexible, out)
        start += len(rows)


async def _migrate_strict_tables(db) -> None:
    """UNSET dead fields, verify no undeclared key remains, define the tables SCHEMAFULL."""
    await _unset_dead_fields(db)
    live = _first(await db.query("INFO FOR DB")).get("tables") or {}
    leftovers: dict[str, dict[str, int]] = {}
    for table in _TABLES:
        if "SCHEMALESS" not in str(live.get(table, "")):
            continue  # WHY: an already-SCHEMAFULL table is read strictly today; TYPE ANY→NORMAL changes no read.
        keys = await _undeclared_keys(db, table)
        if keys:
            leftovers[table] = dict(keys.most_common(20))
    if leftovers:
        logger.error("strict_tables: undeclared row keys remain, no table flipped: %s", leftovers)
        raise MigrationDeferred(f"undeclared row keys remain: {leftovers}")
    for table in _TABLES:
        await db.query(f"DEFINE TABLE OVERWRITE {table} SCHEMAFULL")
    logger.info("strict_tables: %d tables defined SCHEMAFULL", len(_TABLES))
