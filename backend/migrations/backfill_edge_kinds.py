"""Backfill doc_mentions.kind — re-run the rebuild for every live document.

# Plan typed-edges (step 1): rows written before the `kind` field carry NONE
# (SurrealDB DEFAULT never retro-fills); the /edges read coalesces NONE →
# 'mention', and this script converges the stored graph to kinds. Re-runs the
# SAME rebuild the flush pipeline uses (mentions.rebuild_doc_mentions), so it
# is idempotent: a second run rebuilds identical pairs and changes nothing.

NOT registered in the migrations runner on purpose (plan: run once on gray,
then prod after deploy): `docker compose exec -T backend python -m
migrations.backfill_edge_kinds [--dry-run]`.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from db import get_db
from mentions import extract_content_edges, rebuild_doc_mentions
from migrations._shared import logger


async def run(db, *, dry_run: bool = False) -> dict[str, int]:
    """Rebuild edges per project, per live document; returns the counts it logs."""
    projects = await db.query(
        "SELECT VALUE meta::id(id) FROM projects"
    )
    stats = {"projects": 0, "documents": 0, "edges": 0}
    for pid in (projects or []):
        rows = await db.query(
            "SELECT meta::id(id) AS id, content FROM documents "
            "WHERE project_id = $pid AND deleted_at IS NONE",
            {"pid": str(pid)},
        )
        project_docs = 0
        project_edges = 0
        for row in (rows or []):
            doc_id = str(row.get("id") or "")
            if not doc_id:
                continue
            pairs = extract_content_edges(row.get("content") or "")
            stats["documents"] += 1
            project_docs += 1
            if dry_run:
                stats["edges"] += len(pairs)
                project_edges += len(pairs)
                continue
            written = await rebuild_doc_mentions(db, "documents", doc_id, row.get("content") or "")
            stats["edges"] += len(written)
            project_edges += len(written)
        stats["projects"] += 1
        logger.info(
            "backfill_edge_kinds%s: project %s — %d document(s), %d edge(s)",
            " (dry run)" if dry_run else "", pid, project_docs, project_edges,
        )
    logger.info(
        "backfill_edge_kinds%s: total %d project(s), %d document(s), %d edge(s)",
        " (dry run)" if dry_run else "",
        stats["projects"], stats["documents"], stats["edges"],
    )
    return stats


async def _main() -> None:
    # WHY: outside the server nothing configures the migrations logger, so the
    # counts lines — the whole output of a --dry-run — would vanish silently
    # (same as migrate_note_links_to_anchors).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    await run(await get_db(), dry_run="--dry-run" in sys.argv[1:])


if __name__ == "__main__":
    asyncio.run(_main())
