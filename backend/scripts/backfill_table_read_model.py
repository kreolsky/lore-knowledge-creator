"""One-shot backfill: documents.content rows still holding table anchors.

documents.content is the GFM read-model (see INVARIANT(persisted) in
ydoc_store.set_content): embeddings, search and no-session export read the row,
not the Y.Doc. Rows last written by set_content(persist=True) before that fix
hold raw `![label](table:id)` anchors, so their table text is invisible to those
readers until the next write heals them. This script heals them now: it
rewrites ONLY `content` from the persisted Y.Doc (expand_tables(load(id))) —
never ydoc_state, never the update log, never updated_at — and enqueues one
embed per healed row, sequentially (orange is a shared host; no burst).

An anchor whose table model is missing stays verbatim (expand_tables
INVARIANT); such rows are reported as `orphan` and left unchanged when the
expansion changes nothing. A row whose text is not exactly the Y.Doc's
anchor-form text is reported as `diverged` and left alone.

Idempotent + safe to re-run: only rows whose content still contains an anchor.

Usage (inside the backend container):
    python /app/scripts/backfill_table_read_model.py --dry-run
    python /app/scripts/backfill_table_read_model.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from pycrdt import Text

from db import extract_id, get_db
from jobs import pool as jobs_pool
from table_serialize import expand_tables
from ydoc_store import load as load_ydoc

logger = logging.getLogger("backfill_table_read_model")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_ANCHOR_SQL = (
    "SELECT meta::id(id) AS id, project_id, content FROM documents "
    'WHERE content CONTAINS "](table:" AND deleted_at IS NONE'
)


async def _heal(db, row: dict, *, dry_run: bool) -> str:
    """Rewrite one row's content from its Y.Doc; returns the outcome label."""
    doc_id = row["id"]
    doc = await load_ydoc(doc_id)
    # INVARIANT(data-loss): heal only a row that IS the Y.Doc's anchor-form text.
    # Why: a missing/empty Y.Doc or a row that diverged from it would otherwise
    # be overwritten with whatever the Y.Doc expands to — possibly "".
    if str(doc.get("content", type=Text)) != row.get("content"):
        return "diverged"
    expanded = expand_tables(doc)
    if expanded == row.get("content"):
        return "orphan"
    if dry_run:
        return "would-heal"
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c",
        {"id": doc_id, "c": expanded},
    )
    project_id = extract_id(row.get("project_id")) or ""
    await jobs_pool.enqueue("embed_document_task", "doc", doc_id, project_id,
                            job_id=f"embed:{doc_id}")
    return "healed"


async def main(dry_run: bool) -> None:
    db = await get_db()
    rows = await db.query(_ANCHOR_SQL) or []
    logger.info("rows with table anchors in content: %d", len(rows))
    counts: dict[str, int] = {}
    for row in rows:
        try:
            outcome = await _heal(db, row, dry_run=dry_run)
        except Exception:
            logger.exception("failed: %s", row.get("id"))
            outcome = "failed"
        counts[outcome] = counts.get(outcome, 0) + 1
        logger.info("%s %s", outcome, row["id"])
    logger.info("done: %s", counts)
    await jobs_pool.close_arq_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
