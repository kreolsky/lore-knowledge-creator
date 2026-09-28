"""Cascade soft-delete for documents."""
# ARCH: Cascade delete walks graph edges (doc_mentions) — see schema.surql.
# ARCH: No transaction() wrappers — all operations are idempotent single queries.
#       SurrealDB transactions are connection-scoped, and concurrent transactions on
#       the shared connection interfere (2nd BEGIN TRANSACTION fails → 500 → frontend
#       rollback restores stale state). Idempotent operations don't need transactions.
# INVARIANT: cascade is eventually consistent, not atomic.  Why: writes span multiple rows/edges with no distributed transaction; partial progress on a crash is recovered by re-running cleanup (idempotent ops), not rolled back. If the process dies between
#       writes, some edges may be gone but their endpoints still read as live until
#       cleanup is re-run. This is acceptable because: (a) soft-delete is driven by
#       the parent entity's deleted_at, not by edge presence — list queries already
#       filter on the parent; (b) cleanup is idempotent — a retry (next list refresh,
#       or manual replay) fully converges; (c) every read path filters
#       `deleted_at IS NONE` on the target table, so orphan rows are invisible to the
#       UI. Do NOT re-introduce transactions to "fix" this — see ARCH note above for
#       why that regresses throughput under concurrent deletes.
# SYSTEM: cascade-delete — graph traversal for soft-delete propagation to children

import asyncio

from surrealdb import AsyncSurreal

from db import validate_record_id


async def _cascade_cleanup_document(db: AsyncSurreal, document_id: str) -> list[str]:
    """Clean up reference-documents, chunks, mentions, chat sessions, checkpoints
    for an already-soft-deleted document.

    Returns list of reference-document IDs that were cascade-deleted.

    # ARCH: Two-phase concurrent execution on the multiplexed SurrealDB connection.
    # Phase 1: parallel reads for reference-document IDs and chat session IDs.
    # Phase 2: parallel writes — ref-documents + their doc_chunk + their
    # chat_sessions, chat_sessions + messages, doc_chunks, checkpoints,
    # doc_mentions. Subquery-style WHERE-IN-SELECT writes are split into explicit
    # ID lists so concurrent writes don't race against each other's filter
    # conditions.
    """
    safe_did = validate_record_id(document_id)

    # ─── Phase 1: concurrent reads ───────────────────────────────────────────
    ref_doc_ids_task = db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE parent_id = $did AND is_reference = true AND deleted_at IS NONE",
        {"did": document_id},
    )
    chat_ids_task = db.query(
        "SELECT VALUE meta::id(id) FROM chat_sessions WHERE document_id = $did AND deleted_at IS NONE",
        {"did": document_id},
    )

    ref_doc_id_rows, chat_id_rows = await asyncio.gather(
        ref_doc_ids_task, chat_ids_task,
    )

    ref_doc_ids = [str(rid) for rid in (ref_doc_id_rows or []) if rid]
    chat_ids = [str(cid) for cid in (chat_id_rows or []) if cid]

    # ─── Phase 2: concurrent writes ──────────────────────────────────────────
    write_tasks: list = [
        # doc_chunks — hard delete, independent.
        db.query("DELETE doc_chunks WHERE document_id = $did", {"did": document_id}),
        # checkpoints — soft delete, independent.
        db.query(
            "UPDATE checkpoints SET deleted_at = time::now() "
            "WHERE document_id = $did AND deleted_at IS NONE",
            {"did": document_id},
        ),
        # doc_mentions — hard delete by record reference, independent.
        db.query(
            f"DELETE doc_mentions WHERE in = type::record('documents', '{safe_did}') "
            f"OR out = type::record('documents', '{safe_did}')"
        ),
    ]

    if ref_doc_ids:
        # Reference-documents (canonical post-refactor form). Their per-chunk
        # embeddings live in doc_chunks keyed by document_id, alongside regular
        # docs — hard-delete here, then soft-delete the rows themselves.
        write_tasks.append(db.query(
            "DELETE doc_chunks WHERE document_id IN $rids", {"rids": ref_doc_ids}
        ))
        ref_doc_exprs = ", ".join(
            f"type::record('documents', '{validate_record_id(rid)}')" for rid in ref_doc_ids
        )
        write_tasks.append(db.query(
            f"UPDATE documents SET deleted_at = time::now() "
            f"WHERE id IN [{ref_doc_exprs}] AND deleted_at IS NONE"
        ))
        # Cascade chat_sessions anchored to those reference-documents.
        write_tasks.append(db.query(
            "UPDATE chat_sessions SET deleted_at = time::now() "
            "WHERE document_id IN $rids AND deleted_at IS NONE",
            {"rids": ref_doc_ids},
        ))

    if chat_ids:
        write_tasks.append(db.query(
            "UPDATE messages SET deleted_at = time::now() "
            "WHERE chat_id IN $cids AND deleted_at IS NONE",
            {"cids": chat_ids},
        ))
        chat_exprs = ", ".join(
            f"type::record('chat_sessions', '{validate_record_id(cid)}')" for cid in chat_ids
        )
        write_tasks.append(db.query(
            f"UPDATE chat_sessions SET deleted_at = time::now() WHERE id IN [{chat_exprs}] AND deleted_at IS NONE"
        ))

    await asyncio.gather(*write_tasks)
    return ref_doc_ids


async def _cascade_delete_document(db: AsyncSurreal, document_id: str) -> None:
    """Soft-delete a document and cascade-cleanup all related entities.

    Used by project-level delete (projects.py) which needs the full cascade
    including the document soft-delete itself.
    """
    await _cascade_cleanup_document(db, document_id)
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": document_id},
    )
