"""Reference routes — public API surface for "references" as a domain concept.

# ARCH: "Reference" is a first-class concept of the public API, not an
# implementation detail. Internally references are stored as `documents` rows
# with is_reference=true and parent_id pointing at the owning document. This
# storage choice unifies content/title/collab/embeddings with regular
# documents — one editor pipeline, one CRDT sync loop, one chunking path.
#
# The /api/references/* surface deliberately hides that internal shape from
# clients: requests speak `reference_id` / `document_id` (the owning doc),
# responses carry `headings`. The frontend never needs to learn that a
# reference is "a document with a flag" — it asks for references on a doc
# and gets them. This abstraction is permanent; do NOT collapse this router
# into /api/documents.
#
# All reads/writes here go through the documents table. There is no `refs`
# access.
# SYSTEM: references — public CRUD over reference-documents (documents.is_reference=true)
"""

from uuid import uuid4

from documents.move import write_reference_host
from documents.service import (
    create_reference_row,
    rename_document,
    resolve_reference_host,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from files_service import serialize_ref_meta
from surrealdb import AsyncSurreal

from access import (
    get_doc_project_id,
    require_document_full,
    require_document_read,
    require_project_full,
    require_project_read,
)
from auth import get_current_user
from db import (
    REF_META_COLUMNS,
    fetch_one,
    get_ancestor_ids,
    get_db,
    record_refs,
    serialize_record,
    validate_record_id,
)
from deps import extract_headings
from event_bus import emit
from mentions import resolve_first_circle
from models import (
    CreateReference,
    DeleteReferencesRequest,
    PatchReference,
    ReferenceMetaResponse,
    ResolveReferencesRequest,
    is_ref_row,
)
from ydoc_store import set_content

router = APIRouter()


# Metadata-only LIST projection: every field the panel/editor metadata needs, but
# NOT `content` (the multi-MB offender fetched 2-3x per doc switch) and NOT
# `headings` (the per-row markdown parse `extract_headings` did on every request).
# The plain column list is centralized in db.REF_META_COLUMNS (the schema-sync
# source of truth, asserted against surreal/schema.surql by test_projection_sync);
# `has_content` is a computed alias appended here — `content` is referenced ONLY in
# this expression, never projected back to the client. INVARIANT: keep REF_META_COLUMNS
# in sync with the Reference TS type's metadata fields (and with `serialize_ref_meta`).  Why: REF_META_COLUMNS is a hand-written SQL list with no compile-time link to the TS type or serializer; drift silently drops a field the frontend Reference depends on (schema-sync surfaces it).
# `unread_for` rides the projection so serialize_ref_meta can compute the
# VIEWER-relative `unread` bool (see SYSTEM: inbox) — it is popped at the serializer,
# never serialized raw.
_REF_META_SELECT = ", ".join(REF_META_COLUMNS) + ", unread_for, string::len(content ?? '') > 0 AS has_content"


def _serialize_ref(row: dict, viewer_id: str | None = None) -> dict:
    out = serialize_record(row, "reference_id")
    if "parent_id" in out:
        out["document_id"] = out.pop("parent_id")
    # see SYSTEM: inbox — viewer-relative unread; the raw recipient id never serializes.
    out["unread"] = bool(viewer_id) and out.get("unread_for") == viewer_id
    out.pop("unread_for", None)
    out["headings"] = extract_headings(out.get("content") or "")
    return out


async def _require_ref_access(reference_id: str, user: dict, *, write: bool = False) -> dict:
    """Fetch reference, validate is_reference flag, check project access.

    # ARCH: Shared guard for all reference endpoints — eliminates repeated
    # fetch → check is_reference → require_project_* boilerplate.
    """
    ref = await fetch_one("documents", reference_id)
    if not ref or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Reference not found")
    if write:
        await require_document_full(reference_id, user)
    else:
        await require_document_read(reference_id, user)
    return ref


async def _apply_content_patch(db, ref: dict, reference_id: str, new_content: str, pid: str) -> None:
    """Persist a REST content PATCH, ignoring the redundant echo while a collab session owns the doc.

    Extracted from patch_reference to keep that handler under the func-length gate.
    INVARIANT: when a collab session with connected clients owns this reference, the REST
    autosave content is a redundant ECHO of the editor's CRDT state — IGNORE it.  Why: the live session is the authority, so the autosave is a redundant echo of the same CRDT state — applying it delete-all+re-inserts on the shared Y.Doc and mints divergent items that double the text on the next sync. Routing
    it through apply_external_content_change does delete-all+insert on the shared Y.Doc,
    creating all-new items that diverge from connected clients → the text doubles on the
    next sync. Collab WS + periodic flush are the sole persisters while clients are
    connected; the REST path remains the fallback when no client is connected. Genuine
    external edits (transcription, checkpoint restore) call apply_external_content_change
    directly. Why: see lessons/2026-05-30.
    """
    from collab.registry import get_active_session
    _sess = get_active_session("doc", reference_id)
    current_content = ref.get("content") or ""
    content_changed = current_content.rstrip() != new_content.rstrip()
    if not (_sess and _sess.clients) and content_changed:
        # INVARIANT(data-loss): never apply an empty-wipe on the no-session
        # REST path — reject instead (documents.update._reject_empty_wipe).
        # Why: a never-bound editor view checkpoints "" while the server holds
        # real text (plan .kilo/plans/empty-checkpoint-wipe.md), and references
        # have no loss-backup — the reject is their only guard. Baseline is
        # non-empty here by content_changed.
        if not new_content.strip():
            from documents.update import _reject_empty_wipe
            await _reject_empty_wipe(reference_id, pid, len(current_content))
        # INVARIANT(corruption): content and ydoc_state are written by ONE
        # canonical writer — set_content(persist=True; documents.update does
        # the same). Why: a bare `SET content` leaves the persisted Y.Doc at
        # the OLD text, and the next collab open loads ydoc_state, so the
        # browser shows the pre-PATCH body while documents.content says
        # otherwise.
        await set_content(reference_id, new_content, persist=True)
        from mentions import rebuild_doc_mentions
        mention_ids = await rebuild_doc_mentions(db, "documents", reference_id, new_content)
        if mention_ids:
            await emit("backlinks_changed_batch", document_ids=mention_ids)
        await emit("content_flushed", entity_type="doc", entity_id=reference_id,
                   project_id=pid, is_reference=True)


async def _apply_archived_patch(db, reference_id: str, archived: bool, pid: str) -> None:
    """Set the staged-delete archive flag + emit reference_updated.

    Extracted from patch_reference to keep that handler under the func-length gate.
    Emits reference_updated (routed by project_ws.py) so every open client dims-in-place
    (archive) or re-fetches (restore — see useReferenceEvents). No content_flushed /
    reference_renamed: archived is a presentational flag, not a content/reparent change.
    The caller's _require_ref_access(write=True) already gated write access.
    """
    await db.query(
        "UPDATE type::record('documents', $id) SET archived = $a, updated_at = time::now()",
        {"id": reference_id, "a": archived},
    )
    await emit("reference_updated", project_id=pid, reference_id=reference_id)


def _archived_sink_key(ref: dict) -> bool:
    """Sort key placing archived refs below live ones.

    INVARIANT: archived references always sort BELOW every live one, in both LIST scopes.
    Why: the sink is the whole point of "Show archived" — a mixed list is indistinguishable
    from no filter at all, and expressing the rule twice is how the two branches drift.
    `archived` is `option<bool>`, so a row predating the field carries NONE → `bool(None)`
    is False → it sorts as live, which is the intended reading of "not archived".
    """
    return bool(ref.get("archived"))


def sort_refs_by_depth_tier(refs: list[dict], ancestor_ids: list[str]) -> list[dict]:
    """Depth-tier sort: own refs first (manual key order), then ancestors by
    proximity, (sort_key, id) ASC within each tier; archived sunk below live
    (stable).

    # WHY: the SINGLE expression of the reference panel order, shared by the
    # authed LIST (list_references) and the anonymous public references surface
    # (public_share.public_references). `ancestor_ids` is [doc, parent, grandparent,
    # ...] — index 0 is the current doc, so its refs surface first. The three
    # `sorted` passes are applied in order of increasing precedence (last wins):
    # manual key order → group by depth tier → archived sink as the outermost key.
    # Why extract: a second copy on the public surface had drifted to "whole subtree,
    # no sort" (plan "public-share-subtree-tree-and-refs-sort"); one function closes
    # the parity gap permanently.
    #
    # The within-tier pass is the persisted MANUAL order ((sort_key or "", id)
    # ASC) — a content edit no longer moves a ref (updated_at is its content
    # version, not its position). `sort_key` may be missing only on rows the
    # reference_sort_keys_backfill migration has not reached (reads as "" → top).
    """
    depth = {doc_id: i for i, doc_id in enumerate(ancestor_ids)}
    refs = sorted(refs, key=lambda r: (r.get("sort_key") or "", r.get("reference_id") or ""))
    refs = sorted(refs, key=lambda r: depth.get(r.get("document_id"), len(depth)))
    # Applied as the OUTERMOST stable key (last sort wins), so archived refs sink
    # below live while preserving the depth→key sub-order above.
    refs = sorted(refs, key=_archived_sink_key)
    return refs


def dedup_refs_by_id(rows: list[dict], viewer_id: str | None = None) -> list[dict]:
    """Serialize + dedup reference rows by reference_id, keeping the first occurrence.

    # WHY: the SINGLE serialize+dedup pass shared by the authed LIST and the
    # anonymous public references surface — the dedup twin of
    # sort_refs_by_depth_tier (always called immediately before it on both
    # surfaces). Why one function: a cyclic parent chain could make
    # get_ancestor_ids repeat a doc (so its refs appear twice); keep-first by
    # reference_id collapses them. Expressing this once stops the two surfaces
    # drifting on dedup semantics (plan "public-share-subtree-tree-and-refs-sort").

    `viewer_id` (see SYSTEM: inbox): the authed surface passes the caller so
    serialize_ref_meta can stamp the viewer-relative `unread` bool; the
    anonymous public surface passes nothing → unread is always False there.
    """
    seen: set[str] = set()
    result: list[dict] = []
    for r in (rows or []):
        serialized = serialize_ref_meta(r, viewer_id=viewer_id)
        rid = serialized["reference_id"]
        if rid not in seen:
            seen.add(rid)
            result.append(serialized)
    return result


async def _search_project_refs(
    db: AsyncSurreal, project_id: str, q: str, archived_clause: str, limit: int, offset: int
) -> list[dict]:
    """Bounded title-match page for pickers (the `q` mode of the project LIST).

    Presence of q (even empty) selects this mode; `updated_at DESC` orders the empty-query
    default page by recency and gives the client a stable tiering base. The archived clause
    stays applied — a picker must not suggest archived refs (linking one is still fine:
    /resolve includes them).
    """
    search_clause = "AND string::lowercase(title) CONTAINS string::lowercase($q) " if q else ""
    return await db.query(
        f"SELECT {_REF_META_SELECT} FROM documents WHERE project_id = $pid "
        "AND is_reference = true AND deleted_at IS NONE "
        f"{archived_clause}"
        f"{search_clause}"
        "ORDER BY updated_at DESC "
        "LIMIT $limit START $offset",
        {"pid": project_id, **({"q": q} if q else {}), "limit": limit, "offset": offset},
    )


@router.get("/api/references", response_model=list[ReferenceMetaResponse])
async def list_references(
    document_id: str | None = Query(None),
    # No-op since the server resolves the project index doc itself (see below).
    # Kept for client compatibility — older clients still send it.
    index_doc_id: str | None = Query(None),
    project_id: str | None = Query(None),
    # Archived refs are hidden from the default LIST and sink
    # to the bottom when included. The "Show archived" panel toggle forwards this flag.
    include_archived: bool = Query(False),
    # Bounded title search for pickers (project branch only). PRESENT but empty =
    # "the N most recently updated" default page; ABSENT = the legacy panel order.
    # Why the presence distinction: the link-suggestions popup sends q=&limit=50 for
    # its default page, while Sidebar/panel callers omit q and keep the manual order.
    q: str | None = Query(None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List references (documents with is_reference=true) for a project or doc.

    For document_id scope, walks ancestors and collects ref-children of each.
    Sort: own refs first, then parent, then grandparent (depth-asc); manual
    order ((sort_key, id) ASC) within each tier — a content edit does not move
    a ref. Archived refs (when include_archived=true) always sort BELOW all
    live refs.
    """
    if document_id:
        await require_document_read(document_id, user)
        doc_proj = await get_doc_project_id(document_id)
    elif project_id:
        await require_project_read(project_id, user)
    else:
        raise HTTPException(status_code=400, detail="document_id or project_id is required")

    ancestor_ids: list[str] = []
    # WHY: the archived filter is the SAME clause on both
    # branches — `AND archived != true` (NONE/`false` rows pass). Kept in a local so the
    # two SQL strings stay in lockstep; a future diverging filter would be a regression.
    archived_clause = "" if include_archived else "AND archived != true "

    if document_id:
        ancestor_ids = await get_ancestor_ids(document_id, doc_proj)
        # Server-side resolve of the project index doc: ALWAYS include it so
        # index-level refs appear for every document, even when the parent_id
        # chain is broken/orphaned. get_ancestor_ids walks parent_id only and can
        # miss the index doc on orphan/broken chains; the old `index_doc_id`
        # request param was the client's workaround, but it broke the in-flight
        # dedup (different scope keys → double fetch). The param is now a no-op,
        # kept only for client compatibility. Why: orphan-chain regression test.
        # INVARIANT (reference-host): this append is the READ-side mirror of
        # resolve_reference_host / the documents_reference_parent_check event — "project
        # level" means parent_id = index_doc_id, and that doc must be in the served
        # ancestor set for EVERY document so a project-level reference is visible from
        # any panel. Without it the host invariant holds (refs are stored) but stay
        # invisible, which is the exact defect the invariant exists to prevent.
        if doc_proj:
            proj = await fetch_one("projects", doc_proj)
            proj_index = proj.get("index_doc_id") if proj else None
            if proj_index and proj_index not in ancestor_ids:
                ancestor_ids.append(proj_index)
        # WHY the `parent_id IN $ids` scan is index-served: idx_documents_parent_only
        # (parent_id, is_reference, deleted_at) turns each ancestor id into an index
        # point-seek, unioned (UnionIndexScan) — bounded to O(ancestor depth) seeks,
        # NOT a full-table scan. Without that index the planner falls back to a
        # TableScan of ALL documents (verified via EXPLAIN; ~84ms on prod at 3098 docs).
        # NOTE: no SQL ORDER BY here — the Python depth-tier sort below owns the
        # final order (tiers + key + archived sink); a SQL ORDER BY would be a no-op.
        rows = await db.query(
            f"SELECT {_REF_META_SELECT} FROM documents WHERE parent_id IN $ids "
            "AND is_reference = true AND deleted_at IS NONE "
            f"{archived_clause}",
            {"ids": ancestor_ids},
        )
    else:
        if q is not None:
            rows = await _search_project_refs(
                db, project_id, q, archived_clause, limit, offset
            )
        else:
            # SQL paginates (LIMIT/START), so the sink and the grouping must be global
            # in SQL, which a post-hoc Python sort over one page cannot do:
            # `archived ASC,` sinks archived refs; `parent_id ASC,` keeps each host's
            # group contiguous; `sort_key ASC, id ASC` is the manual order within a
            # group. The `_archived_sink_key` pass below is then a no-op re-affirmation
            # — it keeps the RULE expressed in exactly one place.
            rows = await db.query(
                f"SELECT {_REF_META_SELECT} FROM documents WHERE project_id = $pid "
                "AND is_reference = true AND deleted_at IS NONE "
                f"{archived_clause}"
                "ORDER BY archived ASC, parent_id ASC, sort_key ASC, id ASC "
                "LIMIT $limit START $offset",
                {"pid": project_id, "limit": limit, "offset": offset},
            )

    result = dedup_refs_by_id(rows, viewer_id=user["user_id"])

    if ancestor_ids:
        result = sort_refs_by_depth_tier(result, ancestor_ids)
        return result[offset:offset + limit]
    result.sort(key=_archived_sink_key)
    return result


@router.post("/api/references/resolve", response_model=list[ReferenceMetaResponse])
async def resolve_references(
    body: ResolveReferencesRequest,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Resolve which of `ids` are live references of the project (validity probe).

    ARCH: the editor's ref: link validity + cross-doc embed resolver. The probe is bounded
    by the ids the OPEN document's text references; the client chunks at 200 (the model
    cap) — no code path loads every reference of a project. Archived refs are INCLUDED (an
    archived reference still exists and its link must not read as broken); soft-deleted
    are absent.
    """
    await require_project_read(body.project_id, user)
    # `id` is a record ref, so bind per-id (db.record_refs). A malformed id is
    # DROPPED, not a 400 — for a probe it is just "missing" (the filter below
    # keeps that posture; record_refs' own ValueError never escapes).
    valid_ids: list[str] = []
    for raw in body.ids:
        try:
            validate_record_id(raw)
        except ValueError:
            continue
        valid_ids.append(raw)
    if not valid_ids:
        return []
    # INVARIANT(security): ids that are not live references of THIS project are ABSENT
    # from the response, never a 404. Why: "missing" is the probe's normal output and must
    # stay indistinguishable from a foreign-project id — `project_id = $pid` filters both
    # the same way, so no existence oracle leaks (documents batch_read's uniform 404 is
    # the CONTENT path's posture, not a probe's).
    refs, params = record_refs("documents", valid_ids)
    rows = await db.query(
        f"SELECT {_REF_META_SELECT} FROM documents "
        f"WHERE id IN [{refs}] AND project_id = $pid "
        "AND is_reference = true AND deleted_at IS NONE",
        {**params, "pid": body.project_id},
    )
    return dedup_refs_by_id(rows, viewer_id=user["user_id"])


@router.get("/api/references/{reference_id}")
async def get_reference(reference_id: str, user: dict = Depends(get_current_user)):
    ref = await _require_ref_access(reference_id, user)
    return _serialize_ref(ref, viewer_id=user["user_id"])


@router.get("/api/references/{reference_id}/links")
async def get_reference_links(reference_id: str, user: dict = Depends(get_current_user)):
    """First-circle entities linked from this reference's content."""
    ref = await _require_ref_access(reference_id, user)
    # ARCH: prefer live-session content over the DB row (mirrors get_document_links /
    # export_document / chat/context.py) — the DB lags the in-memory Y.Doc by the ~1s
    # flush. References use collab entity_type "doc" (see RELATE below / line ~182).
    from collab.registry import get_active_session
    content = ref.get("content") or ""
    session = get_active_session("doc", reference_id)
    if session and session.clients:
        content = session.content
    return await resolve_first_circle(content, reference_id)


@router.post("/api/references")
async def create_reference(body: CreateReference, user: dict = Depends(get_current_user)):
    await require_project_full(body.project_id, user)
    # "project level" has ONE representation — parent_id = index_doc_id. An empty/
    # omitted document_id means project level; resolve it to the index doc so the
    # reference is visible from every document's panel (never a silent no-host write).
    # INVARIANT: paired with the documents_reference_parent_check schema event.  Why: the app-level host write is backed by a schema event so the parent invariant can't be bypassed by a stray write — enforcement sits at the DB layer, not just this handler.
    host_id = await resolve_reference_host(body.document_id, body.project_id)
    ref_id = str(uuid4())
    try:
        record = await create_reference_row(
            ref_id=ref_id, project_id=body.project_id, host_id=host_id,
            title=body.title, media_type=body.media_type,
            content=body.content, source_url=body.source_url,
            created_by=user.get("user_id"), created_by_name=user.get("name"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _serialize_ref(record, viewer_id=user["user_id"])


@router.patch("/api/references/{reference_id}")
async def patch_reference(reference_id: str, body: PatchReference, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    ref = await _require_ref_access(reference_id, user, write=True)
    pid = ref.get("project_id", "")

    # WHY: content applies LAST — the empty-wipe guard inside it raises 409,
    # and the sibling field updates must not be dropped by a refused content
    # part. Why: a 409 carrying an unapplied rename/move is a silent partial
    # loss the client cannot recover from.
    if body.title is not None:
        # Title routes through the ONE rename core (documents.service
        # .rename_document): 404/400 validation, strip, no-op exit, UPDATE +
        # reference_renamed + content_flushed(is_reference=True) all live there.
        # A second fetch_one + a no-op exit that skips the emit — harmless, the
        # price of one core for both PATCH surfaces (plan rename-core-one-path).
        await rename_document(document_id=reference_id, title=body.title)
    if "document_id" in body.model_fields_set:
        # Empty document_id = "move to project level" → re-host on the index doc.
        # INVARIANT: paired with the documents_reference_parent_check schema event.  Why: the app-level host write is backed by a schema event so the parent invariant can't be bypassed by a stray write — enforcement sits at the DB layer, not just this handler.
        new_parent = await resolve_reference_host(body.document_id, pid)
        # The ONE host writer (documents.move.write_reference_host — the same
        # writer a tree move uses): lands the ref at the TOP of the new host's
        # ref group and returns the key, so no raw parent_id UPDATE here.
        new_key = await write_reference_host(db, reference_id, pid, new_parent)
        await emit(
            "reference_moved", project_id=pid, reference_id=reference_id,
            document_id=new_parent, sort_key=new_key,
        )
    if "archived" in body.model_fields_set and body.archived is not None:
        await _apply_archived_patch(db, reference_id, body.archived, pid)
    if body.content is not None:
        await _apply_content_patch(db, ref, reference_id, body.content, pid)
    updated = await fetch_one("documents", reference_id)
    return _serialize_ref(updated, viewer_id=user["user_id"]) if updated else {}


@router.post("/api/references/batch-delete")
async def delete_references_batch(
    body: DeleteReferencesRequest,
    user: dict = Depends(get_current_user),
):
    """Batch soft-delete multiple references via the unified documents path."""
    # DEBT: a bulk "purge archive" endpoint (hard-delete every archived ref in one
    # call, or every archived ref for a project) would hook here, gated on
    # `WHERE archived = true`.
    # Why deferred: user decision — "решаем по факту" (decide when the need is real).
    # Soft-delete (deleted_at + cascade) stays the per-ref path via
    # DELETE /references/{id}; nothing hard-deletes today.
    from documents.delete import delete_documents_batch_command
    return await delete_documents_batch_command(body.reference_ids, user)


@router.delete("/api/references/{reference_id}")
async def delete_reference(reference_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Soft-delete a single reference-document and cascade-clean related entities."""
    ref = await _require_ref_access(reference_id, user, write=True)
    from cascade import _cascade_delete_document
    pid = ref.get("project_id", "")
    await _cascade_delete_document(db, reference_id)
    await emit("entity_deleted", entity_type="doc", entity_id=reference_id)
    await emit("reference_deleted", project_id=pid, reference_id=reference_id)
    return {"success": True}
