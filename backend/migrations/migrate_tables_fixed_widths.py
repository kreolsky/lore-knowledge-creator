"""Migration: tables_fixed_widths — every legacy ``w: 0`` ("auto") column gets a fixed px width.

see SYSTEM: table-block (backend/table_serialize.py). Per document, every v2 table whose
columns hold a ``0`` width is rewritten through ``freeze_auto_widths`` (the birth rule);
columns that already hold px are never touched. A v1 table awaiting ``tables_v2`` is
skipped — its widths are carried by that migration and frozen on the next boot.

The rewrite is ONE transaction under ``MIGRATION_CLIENT_ID`` and is published through the
backplane (``publish_doc_update``, append=True) so a live session merges it.

Idempotent: a document with no zero width is a no-op. Runs at boot from the runner; by
hand, ``--dry-run`` prints per-document counts without writing:
    docker compose exec backend python -m migrations.migrate_tables_fixed_widths --dry-run
"""

from __future__ import annotations

import asyncio
import logging
import sys

from pycrdt import Doc, Map

from db import get_db
from migrations._shared import logger
from table_serialize import (
    _table_columns_and_rows,
    _v2_parts,
    freeze_auto_widths,
    ordered_axis_ids,
    table_awaits_migration,
)
from ydoc_store import load, publish_doc_update

# WHY: above 2**32 (no Yjs browser client can pick it) and distinct from
# ydoc_store.SEED_CLIENT_ID and the tables_v2 migration's client id.
MIGRATION_CLIENT_ID = 10_000_000_002


def freeze_doc_widths(doc: Doc) -> tuple[int, bytes | None]:
    """Plan one document: (columns rewritten, update to publish or None). ``doc`` is not mutated."""
    work = Doc(client_id=MIGRATION_CLIENT_ID)
    work.apply_update(doc.get_update())
    tables = work.get("tables", type=Map)
    plan: list[tuple[Map, str, int]] = []
    for table in tables.values():
        parts = _v2_parts(table)
        if parts is None or table_awaits_migration(table):
            continue
        widths, matrix = _table_columns_and_rows(table)
        if 0 not in widths:
            continue
        cols = parts[0]
        for cid, old, new in zip(ordered_axis_ids(cols), widths, freeze_auto_widths(widths, matrix)):
            if old != new:
                plan.append((cols[cid], cid, new))
    if not plan:
        return 0, None
    before = work.get_state()
    with work.transaction():
        for col, _, new in plan:
            col["w"] = new
    return len(plan), work.get_update(before)


async def run(db, *, dry_run: bool = False, publish=publish_doc_update) -> dict[str, int]:
    """Freeze every document's zero widths; returns the counts it logs."""
    # WHY every document (trashed ones too): a trashed document can be restored.
    doc_ids = await db.query("SELECT VALUE meta::id(id) FROM documents") or []
    stats = {"documents": 0, "columns": 0}
    for doc_id in sorted(doc_ids):
        columns, update = freeze_doc_widths(await load(doc_id))
        if update is None:
            continue
        stats["documents"] += 1
        stats["columns"] += columns
        logger.info("tables_fixed_widths %s: %d column(s)", doc_id, columns)
        if not dry_run:
            await publish(doc_id, update, append=True)
    logger.info(
        "tables_fixed_widths%s: %d document(s), %d column(s)",
        " (dry run)" if dry_run else "", stats["documents"], stats["columns"],
    )
    return stats


async def _migrate_tables_fixed_widths(db) -> None:
    """Runner entry."""
    await run(db)


async def _main() -> None:
    # WHY: outside the server nothing configures the migrations logger, so the counts
    # lines `run` logs — the whole output of a --dry-run — would vanish silently.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    await run(await get_db(), dry_run="--dry-run" in sys.argv[1:])


if __name__ == "__main__":
    asyncio.run(_main())
