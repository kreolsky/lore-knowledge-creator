"""Doc-command update — update/reorder commands + the PATCH no-session content fallback.

Subsystem overview and ARCH notes live in documents/__init__.py.
See SYSTEM: documents (entry: backend/documents/__init__.py).
# ARCH: PATCH routes to active collab session if exists — session content is
# authoritative.
"""

import logging

from fastapi import HTTPException
from sort_keys import key_between

import event_bus
from access import get_doc_project_id
from db import fetch_one, get_db, serialize_record
from deps import extract_headings
from documents.move import move_document_command
from documents.service import rename_document, set_prefs_last_doc, sibling_rows
from models import PatchDocument, is_ref_row
from ydoc_store import set_content

logger = logging.getLogger(__name__)


async def _live_baseline(document_id: str, existing: dict | None) -> tuple[str, dict | None]:
    """Anchor-form baseline (content, tables_json) from the persisted Y.Doc.

    WHY: the comparison + baseline are BOTH the raw anchor-form
    text from the Y.Doc, NEVER documents.content. Why: documents.content is the
    GFM-derived read-model where expand_tables already inlined each table as a
    pipe table. Comparing it against the anchor-form body.content counted the
    table expansion itself as a change → spurious "(N chars lost)" backups — and
    passing it as baseline_content stored a GFM baseline, so a restore rebuilt
    dead, non-editable pipe text. capture_live_state reads content text + tables
    from the SAME persisted Y.Doc (no live session here), so anchors ↔ table ids
    stay paired AND a table-expansion-only difference is correctly a no-op.

    Y.Doc load failed: degrade to the derived form rather than 500-ing the
    PATCH. This is strictly no-worse than the pre-fix behavior (rare path —
    only when the persisted CRDT state is unreadable). Surface it so the
    degradation is observable, not silent.
    """
    from ydoc_store import capture_live_state
    try:
        return await capture_live_state(document_id)
    except Exception:
        logger.warning(
            "capture_live_state failed in PATCH baseline; degrading to derived content for %s",
            document_id, exc_info=True,
        )
        return (existing.get("content") or "") if existing else "", None


async def _rebuild_mentions(db, document_id: str, project_id: str, content: str) -> None:
    """Rebuild mention edges for new content, then signal the flush."""
    from mentions import rebuild_doc_mentions
    mention_ids = await rebuild_doc_mentions(db, "documents", document_id, content)
    if mention_ids:
        await event_bus.emit("backlinks_changed_batch", document_ids=mention_ids)

    await event_bus.emit("content_flushed", entity_type="doc", entity_id=document_id, project_id=project_id)


async def _reject_empty_wipe(entity_id: str, project_id: str, baseline_len: int) -> None:
    """Reject a REST empty-wipe PATCH: telemetry + warning + 409.

    INVARIANT(data-loss): a REST PATCH replacing a NON-empty entity with
    empty/whitespace-only content while NO live session must fail loudly.
    Why: the editor blocks input while unsynced, so every such PATCH is a client
    bug (the empty-view checkpoint wipe, .kilo/plans/empty-checkpoint-wipe.md);
    the accepted wipe writes documents.content="" while leaving ydoc_state
    untouched (next collab load resurrects or diverges), and the pre-wipe
    content-loss backup is suppressed by the auto_backup adjacent-dedup when the
    safety-open checkpoint holds the same copy — the exact victim signature
    (content_len: 0, zero edit history). Partial big deletes are NOT this path —
    they stay on the loss-backup logic.
    """
    logger.warning(
        "Refusing empty-wipe REST PATCH on %s (baseline %d chars, no live session)",
        entity_id, baseline_len,
    )
    try:
        from telemetry_store import record_telemetry_events
        await record_telemetry_events([{
            "category": "documents",
            "kind": "empty_wipe_rejected",
            "user_id": "",
            "project_id": project_id,
            "entity_id": entity_id,
            "detail": {"baseline_len": baseline_len},
        }])
    except Exception:
        # Telemetry is diagnostic; the 409 below is the safety action and must
        # not be swallowed by a telemetry outage.
        logger.warning("empty_wipe_rejected telemetry record failed", exc_info=True)
    raise HTTPException(
        status_code=409,
        detail="refusing to wipe non-empty document via REST without a live session",
    )


async def _baseline_or_reject(
    document_id: str, project_id: str, new_content: str, existing: dict | None,
) -> tuple[str, str | None] | None:
    """Anchor-form baseline for the PATCH apply path, rejecting empty-wipes.

    Returns None when the PATCH is a no-op (content equal), the
    (baseline_content, baseline_tables_json) pair otherwise.
    INVARIANT(data-loss): never apply an empty-wipe on the no-session REST path.
    Why: a view born before its yCollab binding reads "" while the server holds
    real text — checkpointing it is the empty-view wipe (see _reject_empty_wipe).
    The baseline is non-empty here by the equality return below.
    """
    current_content, baseline_tables_json = await _live_baseline(document_id, existing)
    if current_content.rstrip() == new_content.rstrip():
        return None
    if not new_content.strip():
        await _reject_empty_wipe(document_id, project_id, len(current_content))
    return current_content, baseline_tables_json


async def _apply_content_update(
    db, document_id: str, project_id: str, new_content: str,
) -> dict | None:
    """Apply a REST content update on the no-live-session fallback path.

    # WHY: when a collab session with connected clients owns this doc,
    # the REST autosave content is a redundant ECHO of the editor's CRDT
    # state — IGNORE it. Why: routing it through the Y.Doc does
    # delete-all+insert on the shared Y.Doc, creating all-new items that
    # diverge from what connected clients hold → the text doubles on the next
    # sync (the "multiplies on every re-enter" bug). Collab WS + periodic
    # flush are the sole persisters while clients are connected; the REST path
    # remains the fallback when no client is connected (collab off/degraded).
    # Genuine external edits (agent, checkpoint, transcription) call
    # apply_external_content_change directly. Why: see lessons/2026-05-30.
    """
    # WHY lazy import: collab.py imports from auto_backup,
    # so we import collab lazily here to break the circular dependency.
    from collab.registry import get_active_session
    _sess = get_active_session("doc", document_id)
    if _sess and _sess.clients:
        return None
    existing = await fetch_one("documents", document_id)
    baseline = await _baseline_or_reject(document_id, project_id, new_content, existing)
    if baseline is None:
        return None
    current_content, baseline_tables_json = baseline
    return await _write_content_update(
        db, document_id, project_id, new_content,
        current_content, baseline_tables_json, existing,
    )


async def _write_content_update(
    db, document_id: str, project_id: str, new_content: str,
    current_content: str, baseline_tables_json: str | None, existing: dict | None,
) -> dict | None:
    """Persist a REST content update: loss backup, ONE canonical write, mentions."""
    # WHY: the REST PATCH path stays INLINE (not the arq worker). Why:
    # this is the no-collab fallback — the client is not on the collab WS, so
    # it surfaces the snapshot only via the synchronous `auto_backup` field in
    # this response. The hot collab-flush path (session.py) is the one that
    # enqueues to the worker.
    # INVARIANT(data-loss): references never auto-backup (loss or handoff). Why:
    # references are media, not authored prose (user rule). Closes the gap
    # where a reference edited via REST (no active collab session) was backed up.
    from auto_backup import maybe_backup_on_content_loss
    backup = await maybe_backup_on_content_loss(
        document_id, new_content, baseline_content=current_content,
        baseline_tables_json=baseline_tables_json,
        is_reference=is_ref_row(existing),
    )
    # INVARIANT(corruption): content and ydoc_state are written by ONE canonical
    # writer — set_content(persist=True). Why: a bare `SET content` leaves the
    # persisted Y.Doc at the OLD text, and the next collab open loads ydoc_state,
    # so the browser shows the pre-PATCH body while documents.content says
    # otherwise. set_content also prunes the update log and converges any
    # registered zero-client session via the backplane publish.
    await set_content(document_id, new_content, persist=True)
    await _rebuild_mentions(db, document_id, project_id, new_content)
    return backup


async def _apply_parent_update(document_id: str, project_id: str, parent_id: str) -> None:
    """Re-parent through the ONE in-project move (documents.move)."""
    # Empty string means "clear parent" (move to root)
    # WHY: a re-parented doc is treated as new for the new parent and goes
    # to the TOP of the new sibling group (after_id=None). Why: user spec 2026-06-03 (newest-first).
    await move_document_command(
        document_id=document_id, parent_id=parent_id or None, after_id=None,
        project_id=project_id,
    )


async def update_document_command(document_id: str, body: PatchDocument) -> dict:
    """Update document fields; returns the serialized row + headings (+auto_backup)."""
    project_id = await get_doc_project_id(document_id)
    db = await get_db()
    auto_backup = None
    # WHY: content applies LAST — the empty-wipe guard inside it raises 409,
    # and the sibling field updates (title, parent) must not be dropped by a
    # refused content part. Why: a 409 carrying an unapplied rename is a
    # silent partial loss the client cannot recover from.
    if body.title is not None:
        # Rename routes through the ONE core (documents.service.rename_document):
        # 404/400 validation, strip, no-op exit, UPDATE + document_renamed +
        # content_flushed all live there. The breadcrumb/_chunk_hash WHY moved
        # with the code.
        await rename_document(document_id=document_id, title=body.title)
    if body.parent_id is not None:
        await _apply_parent_update(document_id, project_id, body.parent_id)
    if body.content is not None:
        auto_backup = await _apply_content_update(db, document_id, project_id, body.content)
    updated = await fetch_one("documents", document_id)
    result = serialize_record(updated, "document_id") if updated else {}
    result["headings"] = extract_headings(updated.get("content") or "") if updated else []
    if auto_backup:
        result["auto_backup"] = auto_backup
    return result


async def reorder_document_command(document_id: str, after_id: str | None) -> dict:
    """Place a document after `after_id` within its own sibling group (null = top).

    The parent-never-changes rule lives in the reorder_document docstring in
    routes/documents.py (the route owns the contract text).
    """
    record = await fetch_one("documents", document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found")
    project_id = record.get("project_id")
    parent_id = record.get("parent_id")

    siblings = [s for s in await sibling_rows(project_id, parent_id) if s["id"] != document_id]
    if after_id is None:
        lo, hi = None, (siblings[0]["sort_key"] if siblings else None)
    else:
        idx = next((i for i, s in enumerate(siblings) if s["id"] == after_id), None)
        if idx is None:
            raise HTTPException(status_code=400, detail="after_id is not a sibling of this document")
        lo = siblings[idx]["sort_key"]
        hi = siblings[idx + 1]["sort_key"] if idx + 1 < len(siblings) else None

    try:
        new_key = key_between(lo, hi)
    except Exception:
        # Degenerate bounds (e.g. two siblings sharing a key after a concurrent drag):
        # surface a clean 409 so the client can refetch + retry instead of a raw 500.
        raise HTTPException(status_code=409, detail="Sibling order is stale; refetch and retry")
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET sort_key = $sk, updated_at = time::now()",
        {"id": document_id, "sk": new_key},
    )
    await event_bus.emit("document_reordered", project_id=project_id, document_id=document_id,
                         parent_id=parent_id, sort_key=new_key)
    updated = await fetch_one("documents", document_id)
    return serialize_record(updated, "document_id") if updated else {}


async def record_doc_open(db, user: dict, project_id: str, document_id: str) -> None:
    """Persist the per-user last-accessed-doc pointer for an intentional open.

    INVARIANT: the owner's last-accessed doc lives on the project record
    (projects.last_accessed_doc_id), NOT in project_members.
    Why: there is exactly one owner per project, and legacy/seed projects
    have no owner membership row — a project_members write would silently
    record nothing for them. Storing it on the project is row-independent
    and never auto-creates a stray "owner: readonly" row (which would leak
    into GET /members). Read back per-role in projects._enrich_projects /
    get_project.

    INVARIANT(security): a public-project viewer with NO explicit membership must
    NOT get a project_members row. Why: public read access is purely
    computed (access.get_project_access is_public branch) and never
    materialized — a 'readonly' row would (a) pollute GET /members with
    people the owner never invited, and (b) persist access after the
    project is made private. The viewer's last-doc lives in
    user_preferences (per-user/per-project UI-state table), decoupling
    UI state from access.
    """
    user_id = user["user_id"]
    project = await fetch_one("projects", project_id)
    is_owner = bool(project and project.get("owner_id") == user_id)
    if is_owner:
        await db.query(
            "UPDATE projects SET last_accessed_doc_id = $did "
            "WHERE id = type::record('projects', $pid)",
            {"pid": project_id, "did": document_id},
        )
    elif project:
        updated = await db.query(
            "UPDATE project_members SET last_accessed_doc_id = $did "
            "WHERE project_id = $pid AND user_id = $uid",
            {"pid": project_id, "did": document_id, "uid": user_id},
        )
        if not updated:
            await set_prefs_last_doc(db, user_id, project_id, document_id)
