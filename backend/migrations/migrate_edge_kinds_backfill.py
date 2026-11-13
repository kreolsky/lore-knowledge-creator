"""Migration: edge_kinds_backfill — stamp `doc_mentions.kind` on edges written before the field.

Rows written before the `kind` field carry NONE (SurrealDB DEFAULT never retro-fills);
the /edges read coalesces NONE → 'mention' (backend/edges.py `_stored_triples`), and this
entry converges the stored graph to kinds. It re-runs the SAME rebuild the flush pipeline
uses (mentions.rebuild_doc_mentions) for every live source document that still holds a
NONE-kind edge, so it is idempotent: a document whose edges are all kinded is skipped, and
a rebuilt one rebuilds identical pairs.
"""

from __future__ import annotations

from db import extract_id
from mentions import rebuild_doc_mentions
from migrations._shared import logger


async def _migrate_edge_kinds_backfill(db) -> None:
    """Rebuild edges of every live document that is the source of a NONE-kind edge."""
    sources = await db.query("SELECT VALUE in FROM doc_mentions WHERE kind IS NONE")
    doc_ids = sorted({extract_id(s) for s in (sources or [])} - {None, ""})
    documents = 0
    edges = 0
    for doc_id in doc_ids:
        rows = await db.query(
            "SELECT VALUE content FROM type::record('documents', $id) WHERE deleted_at IS NONE",
            {"id": doc_id},
        )
        if not rows:
            continue
        written = await rebuild_doc_mentions(db, "documents", doc_id, rows[0] or "")
        documents += 1
        edges += len(written)
    logger.info(
        "edge_kinds_backfill: %d document(s) rebuilt, %d edge(s)", documents, edges,
    )
