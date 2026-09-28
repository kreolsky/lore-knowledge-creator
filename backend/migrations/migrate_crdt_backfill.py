"""Data migration: build Y.Doc snapshots + relative-position anchors for CRDT cutover.

Plan: ~/.claude/plans/peaceful-zooming-jellyfish.md (PR 2).

ARCH: One-time data migration. Schema additions ship in schema.surql (idempotic
DEFINE ... IF NOT EXISTS) and apply on every backend boot. This runner is invoked
explicitly (CLI) and rewrites data once.

Idempotency: safe to re-run. Documents that already have ydoc_state are skipped.
Note anchors that already have anchor_rel_start are skipped.

Usage (run against a DB copy first — never -v against production):
    docker compose exec backend python -m migrations.migrate_crdt_backfill
"""

from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "/app")

from pycrdt import Doc, Text

from db import get_db

logger = logging.getLogger("migrate_crdt_backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


async def _build_ydoc_snapshot(content: str | None) -> bytes:
    # WHY: backfill snapshots through the same deterministic seed as live
    # loads (ydoc_store.SEED_CLIENT_ID). A random client_id here would diverge
    # from a live cold-seed and duplicate content on the first sync. Why: see
    # ydoc_store.SEED_CLIENT_ID / lessons/2026-05-30.
    from ydoc_store import seed_doc_from_content
    return seed_doc_from_content(content or "").get_update()


async def _build_sticky_index(doc: Doc, text: Text, offset: int | None) -> bytes | None:
    # INVARIANT: this backfills anchor_rel_* as a pycrdt StickyIndex (binary), which  Why: the backfill uses pycrdt's binary StickyIndex, not the Yjs RelativePosition JSON live note creation stores — they differ, but anchor_rel_* is only a presence hint today, so the mismatch is an accepted residual.
    # differs from the Yjs RelativePosition JSON that live note creation stores
    # (routes/chat/sessions.py). anchor_rel_* is only a presence hint today and is
    # never decoded, so the mismatch is latent — reconcile the two encodings before
    # any code resolves it to an offset. See review 2026-05-29.
    if offset is None or len(text) == 0:
        return None
    clamped = min(offset, len(text))
    if clamped < 0:
        return None
    si = text.sticky_index(clamped)
    return si.encode()


async def migrate_documents(db) -> dict[str, int]:
    result = await db.query(
        "SELECT id, content FROM documents WHERE ydoc_state IS NONE AND deleted_at IS NONE"
    )
    docs = result if result else []
    migrated = 0
    errors = 0

    for doc_row in docs:
        doc_id = doc_row["id"]
        content = doc_row.get("content")
        try:
            snapshot = await _build_ydoc_snapshot(content)
            await db.query(
                "UPDATE type::record('documents', $id) SET ydoc_state = $state",
                {"id": doc_id, "state": snapshot},
            )
            migrated += 1
        except Exception as e:
            errors += 1
            logger.error("Failed to build ydoc_state for document %s: %s", doc_id, e)

    return {"migrated": migrated, "errors": errors, "total": len(docs)}


async def migrate_note_anchors(db) -> dict[str, int]:
    result = await db.query(
        "SELECT id, document_id, anchor_offset_start, anchor_offset_end "
        "FROM chat_sessions "
        "WHERE is_note = true AND anchor_rel_start IS NONE "
        "AND anchor_offset_start IS NOT NONE AND deleted_at IS NONE"
    )
    notes = result if result else []
    migrated = 0
    errors = 0

    for note in notes:
        note_id = note["id"]
        document_id = note.get("document_id")
        offset_start = note.get("anchor_offset_start")
        offset_end = note.get("anchor_offset_end")

        if not document_id:
            continue

        try:
            doc_result = await db.query(
                "SELECT content, ydoc_state FROM type::record('documents', $did)",
                {"did": document_id},
            )
            doc_rows = doc_result if doc_result else []
            if not doc_rows:
                errors += 1
                logger.warning("Document %s not found for note %s", document_id, note_id)
                continue

            doc_row = doc_rows[0]
            ydoc_state = doc_row.get("ydoc_state")
            content = doc_row.get("content", "") or ""

            doc = Doc()
            text = doc.get("content", type=Text)
            if ydoc_state:
                doc.apply_update(ydoc_state)
            elif content:
                text += content

            rel_start = await _build_sticky_index(doc, text, offset_start)
            rel_end = await _build_sticky_index(doc, text, offset_end)

            await db.query(
                "UPDATE type::record('chat_sessions', $id) SET "
                "anchor_rel_start = $rs, anchor_rel_end = $re",
                {"id": note_id, "rs": rel_start, "re": rel_end},
            )
            migrated += 1
        except Exception as e:
            errors += 1
            logger.error("Failed to migrate note anchors for %s: %s", note_id, e)

    return {"migrated": migrated, "errors": errors, "total": len(notes)}


async def run_migration():
    logger.info("Starting CRDT backfill migration...")
    db = await get_db()

    doc_stats = await migrate_documents(db)
    logger.info(
        "Documents: %d/%d migrated (%d errors)",
        doc_stats["migrated"],
        doc_stats["total"],
        doc_stats["errors"],
    )

    anchor_stats = await migrate_note_anchors(db)
    logger.info(
        "Note anchors: %d/%d migrated (%d errors)",
        anchor_stats["migrated"],
        anchor_stats["total"],
        anchor_stats["errors"],
    )

    if doc_stats["errors"] > 0 or anchor_stats["errors"] > 0:
        logger.error("Migration completed with errors — review logs above")
        sys.exit(1)
    else:
        logger.info("Migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
