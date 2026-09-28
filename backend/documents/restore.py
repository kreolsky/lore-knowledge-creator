"""Restore a document from a checkpoint, refreshing its one before-restore backup first."""

import logging
from uuid import uuid4

from auto_backup import LABEL_BEFORE_RESTORE, validate_checkpoint_integrity
from cp_store import (
    BLOB_UNAVAILABLE_DETAIL,
    BlobUnavailable,
    checkpoint_row_fields,
    resolve_checkpoint_content,
)
from fastapi import HTTPException

import event_bus
from db import extract_id, fetch_one, serialize_record
from deps import json_safe

logger = logging.getLogger(__name__)


async def restore_checkpoint_command(db, checkpoint_id: str, checkpoint: dict) -> dict:
    """Restore an already-gated checkpoint's document; the caller fetched the row.

    Order: integrity gate → content resolution → before-restore backup (row +
    event) → the restored content through one writer.
    """
    document_id = checkpoint["document_id"]
    await _assert_restorable(checkpoint_id, checkpoint)
    new_content = await _resolve_restore_content(checkpoint_id, checkpoint)

    # WHY (before-restore): capture the CURRENT doc state for the safety backup
    # from the live Y.Doc, NOT documents.content. Why: documents.content is the derived
    # GFM read-model (anchors → tables); the backup must hold raw anchor text + the full
    # tables subtree so undoing the restore rebuilds identical table blocks. Content and
    # tables are captured from the SAME loaded Y.Doc so anchors and table ids stay paired.
    # Shares capture_live_state with create_checkpoint's content=None fallback (no drift).
    from ydoc_store import capture_live_state

    current_content, current_tables_json = await capture_live_state(document_id)
    backup_id = await _write_before_restore_backup(
        db, document_id, current_content, current_tables_json,
    )
    await _emit_backup_created(document_id, backup_id, current_content, current_tables_json)
    await _apply_restored_content(db, document_id, new_content, checkpoint.get("tables_json"))
    return {"success": True}


async def _assert_restorable(checkpoint_id: str, checkpoint: dict) -> None:
    """503 for an unreadable blob, 409 for a corrupted checkpoint; legacy passes."""
    # INVARIANT(corruption): never restore a checkpoint whose content hash does not match its
    # stored hash. Why: a storage-corrupted checkpoint must not overwrite good
    # document content (no silent degradation). Validate BEFORE writing the safety
    # _backup or touching the document, reusing the already-fetched record.
    integrity = await validate_checkpoint_integrity(checkpoint_id, record=checkpoint)
    # WHY (ordering CRITICAL): a transient blob-read failure returns valid: None with
    # reason "blob_unreadable". This check MUST come BEFORE the legacy `valid is None
    # → proceed` branch below — blob_unreadable also has valid: None, and proceeding
    # would then fail resolving the blob mid-restore (regression). It is NOT
    # corruption (which is valid: False → 409); a transient/missing blob is 503.
    if integrity.get("reason") == "blob_unreadable":
        raise HTTPException(status_code=503, detail="checkpoint_temporarily_unavailable")
    if integrity.get("valid") is False:
        raise HTTPException(status_code=409, detail="checkpoint_corrupted")
    if integrity.get("valid") is None:
        # Legacy checkpoints predate hashing — blocking them would make old history
        # un-restorable. Proceed, but record the gap. (reason here is "no_hash", NOT
        # "blob_unreadable" — that case raised above.)
        logger.warning("Restoring checkpoint %s without integrity hash (legacy)", checkpoint_id)


async def _resolve_restore_content(checkpoint_id: str, checkpoint: dict) -> str:
    """The checkpoint body; 502 when its blob is gone and no inline content exists."""
    # Resolve checkpoint content via the single shared helper.
    # When content_ref is set and resolution fails with NO inline content, the
    # helper raises BlobUnavailable → 502 instead of silently restoring an empty doc.
    try:
        return await resolve_checkpoint_content(checkpoint)
    except BlobUnavailable as e:
        logger.warning("Blob resolution failed for checkpoint %s ref %s", checkpoint_id, e, exc_info=True)
        raise HTTPException(status_code=502, detail=BLOB_UNAVAILABLE_DETAIL)


async def _write_before_restore_backup(
    db, document_id: str, current_content: str, current_tables_json: str | None,
) -> str:
    """Refresh the document's one _backup row (or create it); returns its id."""
    # WHY: exactly one _backup checkpoint per document — reuse if exists,
    # create if not. This keeps the checkpoint list clean while guaranteeing
    # the user can always undo the last restore.  Why: a fresh backup row per restore would bloat the checkpoint list; reuse-or-create keeps exactly one undo target per document.
    backup_rows = await db.query(
        "SELECT id FROM checkpoints WHERE document_id = $did AND label = $label AND deleted_at IS NONE",
        {"did": document_id, "label": LABEL_BEFORE_RESTORE},
    )
    backup_row = backup_rows[0] if backup_rows else None

    # Compute the shared field set (row identity + content_ref/content_hash +
    # tables_json/tables_hash) OUTSIDE the transaction (put_content blob I/O is global,
    # not transactional), then run the row write INSIDE the tx alongside the documents
    # restore (atomicity preserved). This is the same builder create_checkpoint uses,
    # so a field added there appears in the _backup row too.
    backup_fields = await checkpoint_row_fields(
        current_content, current_tables_json,
        document_id=document_id, label=LABEL_BEFORE_RESTORE,
        comment="Auto-backup before restore", created_by=None,
    )

    if backup_row:
        # The backup-row refresh is a SINGLE checkpoint UPDATE — no transaction
        # wrapper needed. The actual content write is routed through exactly ONE
        # writer (_apply_restored_content: apply_external_content_change /
        # set_content), so this branch never writes documents.content.  Why: content has exactly one writer (apply_external_content_change / set_content); checkpoints no longer double-write derived text — they delegate, so single-writer literally holds on both branches.
        # INVARIANT(persisted): no inline content — text lives only in content_ref.  Why: the checkpoint row stores body text only via content_ref (a pointer), never an inline content field — keeping the heavy text out of the row avoids duplicating it and matches the single-writer content shape.
        backup_id = extract_id(backup_row["id"])
        backup_set = (
            ", ".join(f"{k} = ${k}" for k in backup_fields)
            + ", created_at = time::now()"
        )
        await db.query(
            f"UPDATE type::record('checkpoints', $bid) SET {backup_set}",
            {"bid": backup_id, **backup_fields},
        )
        return backup_id
    # Single CREATE — already atomic, no transaction wrapper needed.
    # INVARIANT(persisted): no inline content — text lives only in content_ref.  Why: the checkpoint row stores body text only via content_ref (a pointer), never an inline content field — keeping the heavy text out of the row avoids duplicating it and matches the single-writer content shape.
    backup_id = str(uuid4())
    content_pairs = ", ".join(f"{k}: ${k}" for k in backup_fields)
    await db.query(
        f"CREATE type::record('checkpoints', $bid) CONTENT {{{content_pairs}}}",
        {"bid": backup_id, **backup_fields},
    )
    return backup_id


async def _emit_backup_created(
    document_id: str, backup_id: str, current_content: str, current_tables_json: str | None,
) -> None:
    """Announce the _backup row so the history panel learns the undo point."""
    # Emit checkpoint_created for the before-restore _backup row so the history
    # panel learns about the undo-restore point without a list reload — consistency
    # with the other 5 creation paths. Runs after BOTH write branches so it fires
    # regardless of which (upsert-tx vs single CREATE) executed; placed before
    # apply_external_content_change so the event fires regardless of restore routing.
    fresh_backup_row = await fetch_one("checkpoints", backup_id)
    if not fresh_backup_row:
        return
    backup_payload = serialize_record(fresh_backup_row, "checkpoint_id")
    # The writer stores content only in the blob (content_ref); attach the
    # captured current content so the live snapshot-created event carries text.
    backup_payload["content"] = current_content
    if current_tables_json is not None:
        backup_payload["tables_json"] = current_tables_json
    # WHY: the _backup row has created_by=None, which the checkpoints route's
    # _resolve_user_name names "System" — the same label list/GET show for it.
    backup_payload["user_name"] = "System"
    await event_bus.emit(
        "checkpoint_created",
        entity_type="doc",
        entity_id=document_id,
        event={"type": "checkpoint_created", "checkpoint": json_safe(backup_payload)},
    )


async def _apply_restored_content(
    db, document_id: str, new_content: str, cp_tables_json: str | None,
) -> None:
    """Write the restored body through the live session, else through set_content."""
    # INVARIANT(corruption): apply the restored content through exactly ONE writer.  Why: restore funnels through the same single writer as edits — a live session → apply_external_content_change keeps the Y.Doc authoritative (flush persists); only with no session is set_content the fallback. With a live
    # collab session, route through apply_external_content_change (mutates the session
    # Y.Doc + broadcasts); the session flush persists. Only when no session exists fall
    # back to set_content (loads a separate Y.Doc, persists, publishes its snapshot).
    # Why: running both double-writes the same logical replace from two Y.Doc instances
    # with different client_ids — the conflicting CRDT histories make the restoring
    # client diverge (restore visible only after reload). Mirror agent.
    # tables_json (from the checkpoint row) rebuilds the tables subtree on restore; None
    # = legacy checkpoint with no tables capture → leave the tables map untouched.
    from collab.events import apply_external_content_change
    from collab.registry import get_active_session
    routed = await apply_external_content_change(
        "doc", document_id, new_content, tables_json=cp_tables_json,
    )
    if routed:
        # INVARIANT(data-loss): persist the restored content synchronously by force-flushing the
        # SAME session Y.Doc that apply_external just mutated. Why: apply_external only
        # marks the session dirty and defers persistence to the 1s flush loop. Without
        # an immediate flush, a reload (or the GET right after restore) in that window
        # reads the stale pre-restore ydoc_state — the restore looks lost. Flushing the
        # session ydoc (not a separate set_content Doc) keeps a single CRDT authority.
        session = get_active_session("doc", document_id)
        if session:
            await session.force_flush()
        return
    from ydoc_store import set_content
    await set_content(document_id, new_content, persist=True, tables_json=cp_tables_json)
    # WHY: the no-session restore branch emits content_flushed at the
    # write site. Why: the routed=True branch persists via the session flush
    # (which emits its own), but this branch's set_content is silent — a
    # restored body would never be re-embedded. apply_external_content_change
    # above stays silent for already_persisted writes BY DESIGN (the emit
    # belongs to the caller that owns the write intent, mirroring
    # jobs/tasks.py's transcription emit) — do not move it inside.
    proj = await db.query(
        "SELECT project_id FROM documents WHERE meta::id(id) = $id",
        {"id": document_id},
    )
    if proj:
        await event_bus.emit("content_flushed", entity_type="doc", entity_id=document_id,
                             project_id=proj[0]["project_id"])
