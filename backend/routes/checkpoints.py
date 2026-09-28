"""Checkpoint routes — CRUD, restore with safety backup."""
# SYSTEM: checkpoint-routes — CRUD and restore for document checkpoints with safety backup
# ARCH: One _backup checkpoint per document, refreshed on every restore — enables safe undo.
# The restore handler is guard + command: the body is documents.restore.

import logging

from auto_backup import validate_checkpoint_integrity
from cp_store import (
    BLOB_UNAVAILABLE_DETAIL,
    BlobUnavailable,
    resolve_checkpoint_content,
)
from cp_store import (
    create_checkpoint as create_checkpoint_record,
)
from documents.restore import restore_checkpoint_command
from fastapi import APIRouter, Depends, HTTPException, Query
from surrealdb import AsyncSurreal

from access import require_document_full, require_document_read
from auth import get_current_user
from db import (
    fetch_many,
    fetch_one,
    get_db,
    serialize_record,
    soft_delete,
)
from deps import json_safe
from event_bus import emit
from models import CreateCheckpoint, PatchCheckpoint

logger = logging.getLogger(__name__)

router = APIRouter()


async def _resolve_user_name(created_by: str | None) -> str:
    """Map a checkpoint's created_by id to a display name; "System" for autos/unknown."""
    if not created_by:
        return "System"
    user_map = await fetch_many("users", [created_by])
    return user_map.get(created_by, {}).get("name") or "System"


@router.get("/api/checkpoints")
async def list_checkpoints(
    document_id: str = Query(...),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List checkpoints for a document with pagination, newest first.

    Does NOT return content — use GET /api/checkpoints/{id} for lazy content fetch.
    Why: returning up to 200 full document copies per panel-open is unacceptable;
    post-migration each would need a separate blob resolution.
    """
    await require_document_read(document_id, user)
    rows = await db.query(
        "SELECT id, document_id, content_ref, label, comment, content_hash, "
        "created_by, created_at, deleted_at FROM checkpoints "
        "WHERE document_id = $did AND deleted_at IS NONE "
        "ORDER BY created_at DESC LIMIT $limit START $offset",
        {"did": document_id, "limit": limit, "offset": offset},
    )
    results = [serialize_record(r, "checkpoint_id") for r in (rows or [])]

    # ARCH: Resolve created_by → user_name in one batched serial query for distinct ids.
    # Why: avoid N+1 per row; shared Surreal conn rule forbids gather. "System" for autos.
    user_ids = list({r["created_by"] for r in results if r.get("created_by")})
    user_map = await fetch_many("users", user_ids) if user_ids else {}

    for r in results:
        cb = r.get("created_by")
        r["user_name"] = (user_map.get(cb, {}).get("name") or "System") if cb else "System"

    return results


@router.get("/api/checkpoints/{checkpoint_id}")
async def get_checkpoint(checkpoint_id: str, user: dict = Depends(get_current_user)):
    """Return a single checkpoint WITH resolved content for preview.

    Resolves content from blob (content_ref) when available, falls back to
    inline content for legacy rows. Read-access guard enforced.
    """
    cp = await fetch_one("checkpoints", checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_read(cp.get("document_id", ""), user)

    # Resolve via the single shared helper — inline fallback for legacy rows,
    # BlobUnavailable (→ 502) when the blob is gone with no inline content. No silent
    # degradation (never returns "" for a post-nullout row).
    try:
        content = await resolve_checkpoint_content(cp)
    except BlobUnavailable as e:
        logger.warning("Blob resolution failed in get_checkpoint for ref %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=BLOB_UNAVAILABLE_DETAIL)

    result = serialize_record(cp, "checkpoint_id")
    result["content"] = content
    # Resolve created_by → user_name so a preview fetch doesn't drop the name (→ "System").
    result["user_name"] = await _resolve_user_name(result.get("created_by"))
    return result


@router.post("/api/checkpoints")
async def create_checkpoint(body: CreateCheckpoint, user: dict = Depends(get_current_user)):
    """Create a named checkpoint (snapshot) of document content."""
    await require_document_full(body.document_id, user)
    content = body.content
    tables_json = body.tables_json
    if content is None:
        # WHY: a server-side create (no content sent) snapshots the LIVE
        # Y.Doc, not documents.content. Why: documents.content is the GFM-derived
        # read-model (anchors → tables inlined); storing it as a checkpoint with
        # tables_json=None would restore orphan/duplicate-table state. Read the raw
        # anchor text + capture_tables_json from the same live Y.Doc so a manual
        # create produces a real restore point (anchors + full table state). Shares
        # capture_live_state with restore_checkpoint's safety backup (no drift).
        from ydoc_store import capture_live_state

        content, captured_tables = await capture_live_state(body.document_id)
        if tables_json is None:
            tables_json = captured_tables

    checkpoint = await create_checkpoint_record(
        document_id=body.document_id,
        content=content,
        tables_json=tables_json,
        label=body.label,
        comment=body.comment,
        created_by=user["user_id"],
    )
    # The writer stores content only in the blob (content_ref), so the serialized
    # row carries content=None. Attach the resolved text so the POST response (and
    # the snapshot-created event) carries the actual content the caller sent/captured.
    checkpoint["content"] = content
    if tables_json is not None:
        checkpoint["tables_json"] = tables_json
    checkpoint["user_name"] = user.get("name", "System")

    safe_cp = json_safe(checkpoint)
    await emit("checkpoint_created", entity_type="doc", entity_id=body.document_id,
               event={"type": "checkpoint_created", "checkpoint": safe_cp})

    return checkpoint


@router.post("/api/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(checkpoint_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Restore document content from a checkpoint, creating a safety backup first."""
    checkpoint = await fetch_one("checkpoints", checkpoint_id)
    if not checkpoint:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_full(checkpoint["document_id"], user)
    return await restore_checkpoint_command(db, checkpoint_id, checkpoint)


@router.patch("/api/checkpoints/{checkpoint_id}")
async def patch_checkpoint(checkpoint_id: str, body: PatchCheckpoint, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Update checkpoint label and/or comment."""
    cp = await fetch_one("checkpoints", checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_full(cp.get("document_id", ""), user)
    # Build one dynamic UPDATE from the provided fields instead of two
    # sequential UPDATEs (label/comment can both be set in a single round-trip).
    updates: dict[str, str] = {}
    if body.label is not None:
        updates["label"] = body.label
    if body.comment is not None:
        updates["comment"] = body.comment
    if updates:
        set_clause = ", ".join(f"{k} = ${k}" for k in updates)
        params = {"id": checkpoint_id, **updates}
        await db.query(
            f"UPDATE type::record('checkpoints', $id) SET {set_clause}",
            params,
        )
    updated = await fetch_one("checkpoints", checkpoint_id)
    if not updated:
        return {}
    result = serialize_record(updated, "checkpoint_id")
    # Resolve content via the single shared helper so PATCH returns the full
    # resource — consistency with GET/POST (content lives only in content_ref).
    # Same 502 contract — no silent empty-content response.
    try:
        result["content"] = await resolve_checkpoint_content(updated)
    except BlobUnavailable as e:
        logger.warning("Blob resolution failed in patch_checkpoint for ref %s", e, exc_info=True)
        raise HTTPException(status_code=502, detail=BLOB_UNAVAILABLE_DETAIL)
    result["user_name"] = await _resolve_user_name(result.get("created_by"))
    return result


@router.delete("/api/checkpoints/{checkpoint_id}")
async def delete_checkpoint(checkpoint_id: str, user: dict = Depends(get_current_user)):
    """Soft-delete a checkpoint."""
    cp = await fetch_one("checkpoints", checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_full(cp.get("document_id", ""), user)
    await soft_delete("checkpoints", checkpoint_id)
    return {"success": True}


@router.get("/api/checkpoints/{checkpoint_id}/validate")
async def validate_checkpoint(checkpoint_id: str, user: dict = Depends(get_current_user)):
    """Verify checkpoint content integrity against stored SHA-256 hash."""
    cp = await fetch_one("checkpoints", checkpoint_id)
    if not cp:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_read(cp.get("document_id", ""), user)

    return await validate_checkpoint_integrity(checkpoint_id, record=cp)
