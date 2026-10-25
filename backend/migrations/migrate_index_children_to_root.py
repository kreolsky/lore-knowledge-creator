"""Migration: index_children_to_root — documents under their project's index
doc move to the project root.

# ARCH: the index document is not a tree parent — the tree renders its row
#   childless ("Project Context"), so a document with parent_id = the index
#   exists but is invisible. Writers now normalise index → root for documents
#   (documents.service.effective_parent_id); this one-time pass moves the
#   pre-existing rows. REFERENCES ARE CARVED OUT: parent_id = index_doc_id is
#   a reference's canonical project-level host (documents_reference_parent_
#   check) and must NOT move. The system tree is untouched — its root is
#   created with parent_id = None.

Key-only rewrite: no updated_at bump (position is not an edit), sort_key kept.
Idempotent: after the first run no document row points at its project's
index, so a second run updates nothing.
"""

from __future__ import annotations

from migrations._shared import logger


async def _migrate_index_children_to_root(db) -> None:
    """Set parent_id = NONE on documents stored under their own project's
    index; log the count and the moved ids (persisted-state rewrites are
    named, not summarised)."""
    projects = await db.query(
        "SELECT meta::id(id) AS pid, index_doc_id FROM projects "
        "WHERE index_doc_id != NONE"
    )
    total = 0
    moved_ids: list[str] = []
    for project in (projects or []):
        index_doc_id = project.get("index_doc_id")
        if not index_doc_id:
            continue
        rows = await db.query(
            "SELECT meta::id(id) AS did FROM documents "
            "WHERE parent_id = $idx AND is_reference = false",
            {"idx": index_doc_id},
        )
        ids = [str(r.get("did")) for r in (rows or [])]
        if not ids:
            continue
        await db.query(
            "UPDATE documents SET parent_id = NONE "
            "WHERE parent_id = $idx AND is_reference = false",
            {"idx": index_doc_id},
        )
        total += len(ids)
        moved_ids.extend(ids)
    logger.info(
        "index_children_to_root: moved %d document(s) to project root: %s",
        total, moved_ids,
    )
