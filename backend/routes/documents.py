"""Document routes — thin HTTP handlers over the doc-command layer.

Command logic (create/patch/reorder/delete, the content batch read, the
last-accessed pointer) lives in backend/documents/ (command modules +
service helpers); these handlers enforce access, then delegate.
See SYSTEM: documents (entry: backend/documents/__init__.py).
"""
from documents.batch_read import fetch_content_batch
from documents.create import create_document_command
from documents.delete import delete_document_command, delete_documents_batch_command
from documents.move import move_document_to_project_command
from documents.service import export_document
from documents.update import (
    record_doc_open,
    reorder_document_command,
    update_document_command,
)
from fastapi import APIRouter, Depends, HTTPException
from history_service import list_history as _list_history
from surrealdb import AsyncSurreal

from access import (
    get_doc_project_id,
    require_document_full,
    require_document_read,
    require_project_full,
)
from auth import get_current_user
from db import extract_id, fetch_many, fetch_one, get_db, serialize_record
from deps import extract_headings
from mentions import resolve_first_circle
from models import (
    CreateDocument,
    DeleteDocumentsRequest,
    DocumentBatchRequest,
    MoveDocumentToProject,
    PatchDocument,
    ReorderDocument,
    is_ref_row,
)

router = APIRouter()


@router.get("/api/documents/{document_id}")
async def get_document(document_id: str, track: bool = False, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Fetch a document; record per-user last-accessed pointer only when track=true.

    INVARIANT: last_accessed_doc_id is updated ONLY on an intentional open (the URL-driven
    DocumentPage passes track=true). Why: auxiliary fetches of the SAME endpoint — hover
    previews, chat Sources, snapshot banner, backlinks — would otherwise clobber the
    pointer, so re-entering a project landed on the last *previewed* doc, not the last
    *opened* one. Access is still validated on every call.

    Optimized: access check + member upsert in fewer queries than separate
    require_project_read + SELECT + UPDATE/CREATE flow.
    """
    record = await fetch_one("documents", document_id)
    if not record:
        raise HTTPException(status_code=404, detail="Document not found")
    project_id = record.get("project_id")
    if project_id:
        # Combined access check + last_accessed update (replaces 4 queries with 2-3)
        from access import get_project_access
        access = await get_project_access(project_id, user)
        if access is None:
            raise HTTPException(status_code=404, detail="Project not found")
    if project_id and track:
        await record_doc_open(db, user, project_id, document_id)
    doc_data = serialize_record(record, "document_id")
    doc_data["headings"] = extract_headings(record.get("content") or "")
    return doc_data


async def _open_bundle_project(project_id: str, user: dict) -> dict | None:
    """Project payload for the cold-open bundle, or None if the row is gone.

    # WHY: carries my_access, exactly as GET /api/projects/{id} does. Why:
    # this bundle is what seeds the store on the bare /docs/<id> cold path, and
    # the frontend's default access level is 'full' — without it a readonly
    # member gets edit affordances until the corrective project fetch lands.
    """
    # Deferred import: route modules cross-import widely; avoid import cycles.
    from access import get_project_access, is_project_root

    proj_rec = await fetch_one("projects", project_id)
    if not proj_rec:
        return None
    project = serialize_record(proj_rec, "project_id")
    project["my_access"] = await get_project_access(project_id, user)
    project["is_owner_like"] = await is_project_root(proj_rec, user)
    return project


@router.get("/api/documents/open/{document_id}")
async def open_document(document_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """One-shot cold-open bundle: {document, references, note_sessions, project}.

    ARCH(plan "public-document-ids"): the bare-URL cold path (/docs/<id>) no
    longer carries project_id in the URL. DocumentPage fires document +
    references + note-sessions CONCURRENTLY (see its ARCH note — the prior
    waterfall committed the document ref-less and flickered). Three of those four
    need project_id, which reading off the document response would serialize —
    reinstating exactly the flicker that ARCH forbids. This endpoint returns the
    whole bundle in one RTT, preserving the concurrent-commit invariant. In-app
    nav keeps the per-id calls (project_id is already in the store), so this is
    an entry-path-only addition.

    Reuses the canonical handlers (each keeps its own access check) — the value
    is one HTTP round trip, not eliding the per-resource guards.
    """
    # Deferred imports: route modules cross-import widely; avoid import cycles.
    from routes.chat.sessions_list import list_sessions
    from routes.references import list_references

    doc = await get_document(document_id=document_id, track=True, user=user, db=db)
    project_id = doc.get("project_id")
    project = await _open_bundle_project(project_id, user) if project_id else None
    # NOTE: the Query(...) defaults on these handlers are FastAPI-resolved only on
    # the HTTP path; calling them directly needs explicit values (a raw Query
    # object as a default would crash on internal arithmetic). Passing the same
    # defaults the HTTP path uses. `db` forwards the injected handle the same way.
    references = await list_references(
        document_id=document_id, project_id=None, index_doc_id=None,
        include_archived=False, limit=200, offset=0, user=user, db=db,
    )
    note_sessions = (
        await list_sessions(
            project_id=project_id, document_id=document_id, is_note=True,
            limit=200, offset=0, with_active_messages=False,
            preferred_session_id=None, user=user, db=db,
        )
        if project_id
        else []
    )
    return {
        "document": doc,
        "references": references,
        "note_sessions": note_sessions,
        "project": project,
    }


@router.post("/api/documents/batch")
async def get_documents_batch(
    body: DocumentBatchRequest,
    user: dict = Depends(get_current_user),
):
    """Batch-fetch content for N document/reference ids in ONE projected SELECT.

    Eliminates the N x ~5-round-trip waterfall that opening a doc with many
    `![...](target)` embeds triggered (each per-id GET ran a full-row fetch incl.
    ydoc_state + repeated project lookups + a markdown parse, all serializing on
    the single shared SurrealDB WS — collab/__init__.py:115). References are
    documents with is_reference=true, so one endpoint covers both transclusion
    branches (docs + text-refs).

    # ARCH: access = ONE require_project_read on the resolved project, not N
    # per-doc checks. Sound because document access IS project access
    # (access.get_document_access). The resolved project is taken from the
    # FIRST id IN REQUEST ORDER that exists — deterministic (NOT arbitrary row
    # order, which would make the result depend on SurrealDB's return order).
    Cross-project ids are filtered out (not 404'd) so a stray id never fails the
    whole batch.

    # INVARIANT(security): an all-missing batch raises 404, NOT 200+[] — so the
    # only distinguishable outcomes match the single-doc posture (require_document_read
    # raises a uniform 404 for both "missing" and "no access"). Why: returning
    # 200+[] for missing vs 404 for exists-but-no-access would expose a 3-state
    # existence/access oracle on document UUIDs.
    # INVARIANT(liveness): merge_live_content is applied per id — without it a
    # just-edited embedded doc shows stale content until the ~1s flush. Mirrors
    # the single GET /documents/{id} + /references/{id} gate.  Why: without the per-id live merge a just-edited embedded doc serves stale persisted text until the ~1s flush; this reads the in-memory CRDT state instead.
    # INVARIANT(corruption): ydoc_state is excluded at the SQL projection level
    # (not just stripped by serialize_record) so the multi-KB CRDT snapshot is
    # never transferred for a content batch.  Why: ydoc_state is the multi-KB internal CRDT binary snapshot; projecting it out at SQL level (not just serialize-time) guarantees it never reaches a batch response — leaking it ships internal CRDT state and bloats every row.
    """
    return await fetch_content_batch(body.ids, user)


@router.get("/api/documents/{document_id}/history")
async def get_document_history(document_id: str, user: dict = Depends(get_current_user)):
    """Return the document change history (creation + first-edit-per-user)."""
    await require_document_read(document_id, user)
    return await _list_history(document_id)


@router.get("/api/documents/{document_id}/export")
async def export_document_endpoint(document_id: str, format: str = "pdf", checkpoint_id: str | None = None, user: dict = Depends(get_current_user)):
    """Export document content as PDF, DOCX or Markdown.

    Rendering (live/collab content resolution, transclusion inlining, converter)
    lives in documents.service.export_document; this route enforces read access
    then delegates.
    """
    await require_document_read(document_id, user)
    return await export_document(document_id, format=format, checkpoint_id=checkpoint_id)


@router.post("/api/documents")
async def create_document_endpoint(body: CreateDocument, user: dict = Depends(get_current_user)):
    """Create a new document. Title and path are auto-generated if omitted.

    When is_reference=true the document is a reference: media_type is required,
    parent_id must point at a non-reference document, and the synthetic _ref
    path is used so reference docs never collide with user-named docs.
    """
    await require_project_full(body.project_id, user)
    return await create_document_command(
        body, user_id=user.get("user_id"), user_name=user.get("name"),
    )


@router.patch("/api/documents/{document_id}")
async def patch_document(document_id: str, body: PatchDocument, user: dict = Depends(get_current_user)):
    """Update document fields. Returns headings when content changes."""
    await require_document_full(document_id, user)
    return await update_document_command(document_id, body)


# ARCH: manual reorder within a sibling group. Backend-authoritative key generation
# (thin-client): the client sends `after_id` (the sibling to land after, null = top),
# the server computes the fractional key strictly between that neighbour and the next.
# Serves BOTH kinds — a reference id reorders it within its host's REFERENCE group
# (same-kind key space; archived refs refused).
@router.patch("/api/documents/{document_id}/reorder")
async def reorder_document(
    document_id: str, body: ReorderDocument, user: dict = Depends(get_current_user)
):
    """Place a document or reference after `after_id` within its own sibling group
    (null = top).

    INVARIANT: reorder never changes parent — `after_id` must be a same-kind
    sibling of the moved row (same project_id, parent_id and is_reference) or
    null. A cross-level or cross-kind `after_id` is rejected with 400. Why: user
    spec — drag reorders within one level only; refs keep the rule (their own
    group, same endpoint).
    """
    await require_document_full(document_id, user)
    return await reorder_document_command(document_id, body.after_id)


@router.get("/api/documents/{document_id}/backlinks")
async def get_document_backlinks(
    document_id: str, user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Return documents that link to this document via doc_mentions edges."""
    await require_document_read(document_id, user)
    doc_project_id = await get_doc_project_id(document_id)
    source_refs = await db.query(
        "SELECT VALUE in FROM doc_mentions "
        "WHERE out = type::record('documents', $did)",
        {"did": document_id},
    )
    src_ids = [extract_id(ref) for ref in (source_refs or [])]
    src_ids = [sid for sid in src_ids if sid]
    if not src_ids:
        return {"backlinks": []}

    doc_map = await fetch_many("documents", src_ids)

    backlinks = []
    for src_id in src_ids:
        if src_id not in doc_map:
            continue
        src_doc = doc_map[src_id]
        # INVARIANT(security): a by-id target outside the reading document's
        # project is skipped. Why: doc_mentions edges survive cross-project
        # subtree moves (documents/move.py never rewrites them), so without the
        # filter a member of the target project would learn the title and
        # updated_at of documents in a project he cannot read. Project equality
        # is the wall — per-source access resolution is deliberately absent
        # (small trusted group; N×4 round-trips otherwise, audit N4).
        if src_doc.get("project_id") != doc_project_id:
            continue
        is_ref = is_ref_row(src_doc)
        entry: dict = {
            "document_id": src_id,
            "title": src_doc.get("title", ""),
            "updated_at": str(src_doc["updated_at"]) if src_doc.get("updated_at") else None,
            "source_type": "ref" if is_ref else "doc",
            "is_reference": is_ref,
        }
        if is_ref:
            # WHY: keep reference_id alias on the response so clients that branch
            # on it (frontend + tests) continue to work.
            entry["reference_id"] = src_id
        backlinks.append(entry)
    return {"backlinks": backlinks}


async def _resolve_first_circle(content: str, self_id: str) -> dict:
    return await resolve_first_circle(content, self_id)


@router.get("/api/documents/{document_id}/links")
async def get_document_links(
    document_id: str, user: dict = Depends(get_current_user)
):
    """Return first-circle entities explicitly linked from this document's body."""
    doc = await fetch_one("documents", document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    await require_document_read(document_id, user)
    # ARCH: prefer live-session content over the DB row so the cascade matches what
    # the chat completion path sees (chat/context.py) and export_document — the DB
    # lags the in-memory Y.Doc by the ~1s flush, so a just-deleted link would
    # otherwise still cascade. Idle session (no clients) → DB is authoritative.
    from collab.registry import get_active_session
    content = doc.get("content") or ""
    session = get_active_session("doc", document_id)
    if session and session.clients:
        content = session.content
    return await _resolve_first_circle(content, document_id)


@router.post("/api/documents/{document_id}/move")
async def move_document_to_project(
    document_id: str, body: MoveDocumentToProject, user: dict = Depends(get_current_user),
):
    """Move a document WITH its whole live subtree to another project.

    # INVARIANT(security): the move is gated at BOTH ends — require_document_full
    # on the moved document (source) AND require_project_full on the target
    # project. Why: a single-ended gate would let a principal with write access
    # to one project INJECT a subtree into a project he has no write access to
    # (the target's tree, path namespace and denormalized rows all change).
    # Both gates are the existing primitives; no new access surface.
    """
    await require_document_full(document_id, user)
    await require_project_full(body.target_project_id, user)
    return await move_document_to_project_command(
        document_id, body.target_project_id, body.parent_id,
    )


@router.delete("/api/documents/{document_id}")
async def delete_document(
    document_id: str,
    delete_children: bool = True,
    user: dict = Depends(get_current_user),
):
    """Soft-delete a document; delete_children picks the mode.

    - delete_children=true (default): the whole subtree (target + live descendants,
      docs AND references) is tombstoned in place — no reparenting — and one
      documents_deleted_batch event is emitted.
    - delete_children=false: legacy lift mode — live non-reference children are
      reparented to the grandparent and per-doc events fire.

    # INVARIANT(security): the access gate splits by mode — subtree gates
    # require_project_full on the target's project, lift keeps require_document_full.
    # Why: a subtree delete writes every descendant, so it is gated on the
    # project the descendants live in. Mirrors the batch command's existing gate.
    # WHY: Reparent, soft-delete, and cascade cleanup run concurrently via asyncio.gather.
    # All three operate on different records/tables and are independent by data, so the
    # SurrealDB SDK multiplexes them over the shared connection. emit() fires after gather
    # so WS clients see a consistent state; emit is itself fire-and-forget.
    """
    if delete_children:
        await require_project_full(await get_doc_project_id(document_id), user)
    else:
        await require_document_full(document_id, user)
    return await delete_document_command(document_id, delete_children=delete_children)


@router.post("/api/documents/batch-delete")
async def delete_documents_batch(
    body: DeleteDocumentsRequest,
    user: dict = Depends(get_current_user),
):
    """Batch soft-delete multiple documents in one request.

    # ARCH: POST (not DELETE) to avoid path conflict with /api/documents/{id} and
    # to allow a request body without CORS/proxy edge cases. All IDs must belong
    # to the same project -- mixed-project arrays are rejected (IDOR guard).
    # Missing IDs are silently skipped. Cascade cleanup runs per-doc via the
    # existing _cascade_cleanup_document helper. delete_children=true (default)
    # expands every selected doc with its live descendants (see
    # delete_documents_batch_command).
    """
    return await delete_documents_batch_command(
        body.document_ids, user, delete_children=body.delete_children,
    )
