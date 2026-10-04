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
    # idempotent=True on every statement: a WS reader death mid-cascade may leave
    # delivery uncertain, and every statement here is a guarded record-targeted
    # write or a read — safe to re-issue (see pool.py's retry contract).
    ref_doc_ids_task = db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE parent_id = $did AND is_reference = true AND deleted_at IS NONE",
        {"did": document_id},
        idempotent=True,
    )
    chat_ids_task = db.query(
        "SELECT VALUE meta::id(id) FROM chat_sessions WHERE document_id = $did AND deleted_at IS NONE",
        {"did": document_id},
        idempotent=True,
    )

    ref_doc_id_rows, chat_id_rows = await asyncio.gather(
        ref_doc_ids_task, chat_ids_task,
    )

    ref_doc_ids = [str(rid) for rid in (ref_doc_id_rows or []) if rid]
    chat_ids = [str(cid) for cid in (chat_id_rows or []) if cid]

    # ─── Phase 2: concurrent writes ──────────────────────────────────────────
    write_tasks: list = [
        # doc_chunks — hard delete, independent.
        db.query(
            "DELETE doc_chunks WHERE document_id = $did",
            {"did": document_id},
            idempotent=True,
        ),
        # checkpoints — soft delete, independent.
        db.query(
            "UPDATE checkpoints SET deleted_at = time::now() "
            "WHERE document_id = $did AND deleted_at IS NONE",
            {"did": document_id},
            idempotent=True,
        ),
        # doc_mentions — hard delete by record reference, independent.
        # WHY: same class as the documents UPDATE below — a `WHERE in = … OR out = …`
        # filter scans the whole edge table, so the graph delete targets the endpoint
        # records (`Iterate Record` per edge).
        db.query(
            "DELETE type::record('documents', $id)->doc_mentions, "
            "type::record('documents', $id)<-doc_mentions",
            {"id": safe_did},
            idempotent=True,
        ),
    ]

    if ref_doc_ids:
        # Reference-documents (canonical post-refactor form). Their per-chunk
        # embeddings live in doc_chunks keyed by document_id, alongside regular
        # docs — hard-delete here, then soft-delete the rows themselves.
        write_tasks.append(db.query(
            "DELETE doc_chunks WHERE document_id IN $rids",
            {"rids": ref_doc_ids},
            idempotent=True,
        ))
        # WHY: a `WHERE id IN [list]` filter scans the whole table and its read set
        # conflicts with any concurrent write to that table (surrealkv checks the
        # read set at commit — the embed worker's per-reference status write then
        # fails the cascade after minutes of scanning), so the cascade targets
        # records: `Iterate Record` per id. A missing id is a no-op.
        write_tasks.append(db.query(
            "UPDATE $rids.map(|$x| type::record('documents', $x)) "
            "SET deleted_at = time::now() WHERE deleted_at IS NONE",
            {"rids": ref_doc_ids},
            idempotent=True,
        ))
        # Cascade chat_sessions anchored to those reference-documents.
        write_tasks.append(db.query(
            "UPDATE chat_sessions SET deleted_at = time::now() "
            "WHERE document_id IN $rids AND deleted_at IS NONE",
            {"rids": ref_doc_ids},
            idempotent=True,
        ))

    if chat_ids:
        write_tasks.append(db.query(
            "UPDATE messages SET deleted_at = time::now() "
            "WHERE chat_id IN $cids AND deleted_at IS NONE",
            {"cids": chat_ids},
            idempotent=True,
        ))
        write_tasks.append(db.query(
            "UPDATE $cids.map(|$x| type::record('chat_sessions', $x)) "
            "SET deleted_at = time::now() WHERE deleted_at IS NONE",
            {"cids": chat_ids},
            idempotent=True,
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
        idempotent=True,
    )
