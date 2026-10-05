"""Admin info routes — instance-level storage stats for the admin Info section.

# ARCH: computed ON REQUEST, never polled and never prewarmed. These numbers
# feed a human looking at Admin → Info; nothing schedules or subscribes to
# them. A full run is seconds of serial per-table scans — fine on demand, wrong
# on a timer. Do not add polling or a background job for this. The last result
# is memoized for STORAGE_CACHE_TTL_S (the data moves slowly) and always
# carries its `measured_at`, so a cached answer is never shown as current;
# `?fresh=1` (the Refresh button) bypasses the memo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from surrealdb import AsyncSurreal

import config
from auth import require_admin
from db import get_db, validate_record_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/info", tags=["admin-info"])

STORAGE_CACHE_TTL_S = 30 * 60

# Process-local memo (per replica): one admin reads this, a restart recomputes
# once. Only successful measurements are stored — a failed scan raises and the
# next request retries.
_storage_cache: dict | None = None
_storage_cache_at: float = 0.0
_storage_lock = asyncio.Lock()

# One statement per table. The inner SELECT derives the group keys and the row
# length; `$this` is refused directly under GROUP BY, hence the subquery.
# `string::len(<string> $this)` is a LOGICAL estimate (serialized row length),
# not disk bytes — surrealkv does not expose per-table disk usage, and engine
# versions + compaction make the file far larger than the logical sum. Never
# present this sum as disk usage.
def _count_sql(table: str, with_project_leg: bool) -> str:
    if with_project_leg:
        return (
            "SELECT del, in_dead_project, count() AS rows, math::sum(b) AS bytes FROM "
            "(SELECT deleted_at IS NOT NONE AS del, project_id IN $dead AS in_dead_project, "
            f"string::len(<string> $this) AS b FROM {table}) "
            "GROUP BY del, in_dead_project"
        )
    return (
        "SELECT del, count() AS rows, math::sum(b) AS bytes FROM "
        "(SELECT deleted_at IS NOT NONE AS del, "
        f"string::len(<string> $this) AS b FROM {table}) "
        "GROUP BY del"
    )


_ZEROS = {
    "live_rows": 0, "live_bytes": 0,
    "deleted_rows": 0, "deleted_bytes": 0,
    "in_deleted_projects_rows": 0, "in_deleted_projects_bytes": 0,
}


def _tree_size(path: Path) -> int:
    """Sum of `st_size` over everything under `path` (file or directory)."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _disk_part() -> tuple[dict | None, str | None]:
    """The live DB directory + the whole volume, in bytes.

    Reads only file SIZES (os.stat) — the DB contents stay inaccessible to
    this endpoint even though the mount holds them.
    """
    db_path: Path = config.SURREAL_DB_DIR
    if not db_path.exists():
        # Missing mount is an explicit error, never 0 — a zero beside live
        # numbers reads as "empty database" and hides a wiring failure.
        return None, f"{db_path} is not mounted"
    return {
        "db_bytes": _tree_size(db_path),
        # The volume holds more than lore.db on a long-lived install (old
        # copies, exports); reporting both makes the gap visible.
        "volume_bytes": _tree_size(db_path.parent),
    }, None


def _bucket(rows: list[dict]) -> dict:
    """Fold GROUP BY rows into the disjoint live / deleted / in-dead-project buckets."""
    out = dict(_ZEROS)
    for r in rows or []:
        n, b = r.get("rows") or 0, r.get("bytes") or 0
        # INVARIANT: the buckets are DISJOINT — deleted = own deleted_at set (any
        # project state); in_deleted_projects = own row live, its project soft-deleted.
        # Why: "how much data is deleted" is the plain sum of the two buckets, no double count.
        if r.get("del"):
            out["deleted_rows"] += n
            out["deleted_bytes"] += b
        elif r.get("in_dead_project"):
            out["in_deleted_projects_rows"] += n
            out["in_deleted_projects_bytes"] += b
        else:
            out["live_rows"] += n
            out["live_bytes"] += b
    return out


async def _dead_project_ids(db: AsyncSurreal) -> list[str]:
    """Bare ids of soft-deleted projects — the $dead set for the project leg."""
    rows = await db.query(
        "SELECT VALUE meta::id(id) FROM projects WHERE deleted_at IS NOT NONE",
    ) or []
    return [r for r in rows if r]


async def _tables_part(db: AsyncSurreal) -> tuple[list[dict], dict]:
    """Per-table stats + totals, serially over every INFO FOR DB table.

    Serial queries, never gather — ONE shared WS connection multiplexes every
    query, and gathered queries contend instead of overlapping.
    """
    infodb = await db.query("INFO FOR DB")
    blob = infodb[0] if isinstance(infodb, list) else infodb
    table_names = sorted(((blob or {}).get("tables") or {}).keys())

    tables: list[dict] = []
    totals = dict(_ZEROS)
    dead_ids: list[str] | None = None
    for name in table_names:
        # Table names come from INFO FOR DB, so interpolation is safe; validated
        # anyway as the one guard before the f-string. An unsafe name raises —
        # a table silently missing from the list would read as "no data".
        validate_record_id(name)
        t0 = time.monotonic()

        info = await db.query(f"INFO FOR TABLE {name}")
        tblob = info[0] if isinstance(info, list) else info
        has_project_field = "project_id" in ((tblob or {}).get("fields") or {})

        sql = _count_sql(name, has_project_field)
        if has_project_field:
            if dead_ids is None:
                dead_ids = await _dead_project_ids(db)
            rows = await db.query(sql, {"dead": dead_ids})
        else:
            rows = await db.query(sql)

        stats = _bucket(rows)
        tables.append({"name": name, **stats})
        for k in totals:
            totals[k] += stats[k]
        logger.info(
            "admin info storage: table %s took %.0f ms",
            name, (time.monotonic() - t0) * 1000,
        )
    return tables, totals


async def _measure(db: AsyncSurreal) -> dict:
    """One full storage measurement, stamped with when it was taken."""
    started = time.monotonic()
    disk, disk_error = _disk_part()
    tables, totals = await _tables_part(db)
    return {
        "disk": disk,
        "disk_error": disk_error,
        "tables": tables,
        "totals": totals,
        "measured_ms": round((time.monotonic() - started) * 1000),
        "measured_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/storage")
async def get_storage(
    fresh: bool = False,
    _: dict = Depends(require_admin),
    db: AsyncSurreal = Depends(get_db),
):
    """Database size on disk + soft-deleted data, table by table (admin only).

    Served from a STORAGE_CACHE_TTL_S memo unless `fresh` is set. A `fresh`
    request that waited on an in-flight run reuses that run — it finished after
    the request was made, and a second scan right behind it would only repeat it.
    """
    global _storage_cache, _storage_cache_at
    asked_at = time.monotonic()
    async with _storage_lock:  # INVARIANT: at most one measurement runs at a time; a waiter reuses its result.
        # Why: each run is seconds of serial scans on the ONE shared DB connection —
        # stacked runs contend with every other query instead of finishing sooner.
        if _storage_cache is not None:
            age = time.monotonic() - _storage_cache_at
            if (not fresh and age < STORAGE_CACHE_TTL_S) or _storage_cache_at >= asked_at:
                return _storage_cache
        _storage_cache = await _measure(db)
        _storage_cache_at = time.monotonic()
        return _storage_cache
