"""Doc-command move — in-project reparent/reorder and cross-project subtree move.

Subsystem overview and ARCH notes live in documents/__init__.py.
See SYSTEM: documents (entry: backend/documents/__init__.py).

# ARCH: move_document_command is the ONE in-project move: the agent move_document
#   tool and the PATCH /api/documents parent_id branch both call it, after their
#   own access gate. It is STRUCTURAL — it never touches the CRDT text.
#
# ARCH (cross-project): all-or-nothing on validation, eventually-consistent on writes (the same
#   contract as cascade.py INVARIANT — SurrealDB transactions are
#   connection-scoped, so a multi-table move must not wrap in one). Ordering
#   makes the partial state safe: documents first (the source of truth every
#   read path filters on), then denormalized tables, then the live-session
#   patch, then events. A crash mid-way leaves doc_chunks/chat_sessions under
#   the old project until the next flush/embed rewrites them.
"""

import asyncio

from agent_config_seed import memory_folder_id
from collab.registry import get_active_session
from fastapi import HTTPException

import event_bus
from db import extract_id, fetch_one, get_db, validate_record_id
from documents.delete import _collect_subtree_ids
from documents.service import (
    ancestor_chain,
    assert_no_cycle,
    assert_parent_valid,
    generate_path_for_title,
    place_after,
    sibling_rows,
    top_sibling_key,
)
from models import is_ref_row
from scope import require_doc_in_scope, resolve_scoped_parent

# Rows needed for the refusal checks + path re-derivation + the event split.
_MOVE_COLUMNS = (
    "id, project_id, parent_id, title, path, is_reference, "
    "is_system, is_memory, is_index"
)


def _is_project_anchor(doc: dict | None) -> bool:
    """A row that belongs structurally to its project and cannot travel:
    the agent/system skeleton, a memory fact, or a project's index doc.
    Widened from delete._is_protected_skeleton's shape (system_role-gated)
    to the three FLAGS — a memory fact or an index doc under a moved subtree
    would strand the source project's brain/entry point in another project."""
    return bool(
        doc
        and (doc.get("is_system") or doc.get("is_memory") or doc.get("is_index"))
    )


async def _collect_live_subtree(db, document_id: str) -> dict[str, dict]:
    """Live subtree rows for the move (docs AND references), keyed by id."""
    subtree_ids = await _collect_subtree_ids(db, document_id)
    in_clause = ",".join(
        f"type::record('documents','{validate_record_id(d)}')" for d in subtree_ids
    )
    rows = await db.query(
        f"SELECT {_MOVE_COLUMNS} FROM documents "
        f"WHERE id IN [{in_clause}] AND deleted_at IS NONE"
    )
    docs: dict[str, dict] = {}
    for r in (rows or []):
        did = extract_id(r.get("id"))
        if did:
            docs[did] = r
    return docs


async def _validate_move(
    db, docs: dict[str, dict], target_project_id: str, parent_id: str | None,
) -> str | None:
    """Structural refusals, ALL before any write. Returns the normalized
    parent_id (None = target project root)."""
    anchored = [did for did, d in docs.items() if _is_project_anchor(d)]
    if anchored:
        raise HTTPException(
            status_code=403,
            detail="System, memory or index documents cannot be moved to another project.",
        )
    parent_row = await assert_parent_valid(parent_id, target_project_id)
    if parent_row is not None and parent_row.get("is_system"):
        # A user subtree under the agent skeleton is not a supported shape.
        raise HTTPException(
            status_code=400,
            detail="A system document cannot be the parent of a moved subtree",
        )
    return parent_id or None


async def _rederive_colliding_paths(
    db, docs: dict[str, dict], target_project_id: str,
) -> list[str]:
    """Re-derive paths for colliding non-reference rows; return the ids that
    need NO path rewrite (references + non-colliding).

    idx_documents_path is UNIQUE on (project_id, path). Colliding rows
    re-derive SEQUENTIALLY — generate_path_for_title reads the target's live
    paths, and each row's own UPDATE lands before the next derive call, so two
    same-title movers get distinct slugs. Reference rows keep their
    _ref/<uuid>.md paths (globally unique by construction, never colliding).
    """
    existing_paths = {
        (r.get("path") or "").lower() for r in (await db.query(
            "SELECT path FROM documents WHERE project_id = $pid "
            "AND is_reference = false AND deleted_at IS NONE",
            {"pid": target_project_id},
        ) or [])
    }
    plain_ids: list[str] = []
    for did, d in docs.items():
        if not is_ref_row(d) and (d.get("path") or "").lower() in existing_paths:
            new_path = await generate_path_for_title(
                target_project_id, d.get("title") or "",
            )
            await db.query(
                "UPDATE type::record('documents', $id) SET project_id = $pid, "
                "path = $path, updated_at = time::now()",
                {"id": did, "pid": target_project_id, "path": new_path},
            )
        else:
            plain_ids.append(did)
    return plain_ids


async def _rewrite_documents(
    db, docs: dict[str, dict], document_id: str,
    target_project_id: str, parent_id: str | None,
) -> None:
    """Documents UPDATE — the FIRST write of the move: subtree project_id,
    then the root's parent_id + sort_key.

    The root lands at the TOP of the target sibling group (same rule as an
    in-project re-parent, move_document_command with after_id=None); descendants keep parent_id + sort_key — their
    sibling groups travel as a whole. The schema fence documents_parent_check
    only rejects reference-as-parent (not project agreement), so the root
    UPDATE needs no ordering vs the bulk one.
    """
    plain_ids = await _rederive_colliding_paths(db, docs, target_project_id)
    if plain_ids:
        plain_clause = ",".join(
            f"type::record('documents','{validate_record_id(d)}')" for d in plain_ids
        )
        await db.query(
            f"UPDATE documents SET project_id = $pid, updated_at = time::now() "
            f"WHERE id IN [{plain_clause}]",
            {"pid": target_project_id},
        )
    new_key = await top_sibling_key(target_project_id, parent_id)
    await db.query(
        "UPDATE type::record('documents', $id) SET parent_id = $par, "
        "sort_key = $sk, updated_at = time::now()",
        {"id": document_id, "par": parent_id, "sk": new_key},
    )


async def _rewrite_denormalized(
    db, source_project_id: str, target_project_id: str, all_ids: list[str],
) -> None:
    """Denormalized project_id rewrites + last-accessed pointer clears, in ONE
    gather.

    UPDATEs: rows carrying a denormalized project_id that every project-scoped
    read filters on. Last-accessed pointers (and the voice-recording destination)
    naming a moved doc are cleared SOURCE-side only: the liveness filter in
    projects.py only drops DELETED pointers, so a live-but-moved one would make
    "enter project A" resolve /documents/open/D into project B — and A's voice
    notes would keep landing on a doc that now lives in B.
    """
    pid = {"pid": target_project_id, "ids": all_ids}
    src = {"pid": source_project_id, "ids": all_ids}
    await asyncio.gather(
        db.query("UPDATE doc_chunks SET project_id = $pid WHERE document_id IN $ids", pid),
        db.query("UPDATE document_shares SET project_id = $pid WHERE document_id IN $ids", pid),
        db.query("UPDATE chat_sessions SET project_id = $pid WHERE document_id IN $ids", pid),
        db.query("UPDATE agent_configs SET project_id = $pid WHERE document_id IN $ids", pid),
        db.query("UPDATE api_keys SET project_id = $pid WHERE document_id IN $ids", pid),
        db.query(
            "UPDATE projects SET last_accessed_doc_id = NONE, voice_recording_doc_id = NONE "
            "WHERE id = type::record('projects', $pid) "
            "AND (last_accessed_doc_id IN $ids OR voice_recording_doc_id IN $ids)", src,
        ),
        db.query(
            "UPDATE project_members SET last_accessed_doc_id = NONE "
            "WHERE project_id = $pid AND last_accessed_doc_id IN $ids", src,
        ),
        db.query(
            "UPDATE user_preferences SET preferences.last_accessed_doc_id = NONE "
            "WHERE project_id = $pid AND preferences.last_accessed_doc_id IN $ids", src,
        ),
    )


async def _emit_move_events(
    source_project_id: str, target_project_id: str,
    document_ids: list[str], reference_ids: list[str],
) -> None:
    """Two broadcasts: OUT to the source project (with the target's id+name so
    a client whose open doc left can toast + follow), IN to the target project
    (a plain tree-refetch signal)."""
    target_project = await fetch_one("projects", target_project_id)
    await event_bus.emit(
        "documents_moved_out",
        project_id=source_project_id,
        document_ids=document_ids,
        reference_ids=reference_ids,
        target_project_id=target_project_id,
        target_project_name=(target_project or {}).get("name") or "",
    )
    await event_bus.emit(
        "documents_moved_in",
        project_id=target_project_id,
        document_ids=document_ids,
    )


async def move_document_to_project_command(
    document_id: str, target_project_id: str, parent_id: str | None,
) -> dict:
    """Move a document WITH its whole live subtree to another project.

    Route-side gates (routes/documents.py): require_document_full on the moved
    doc + require_project_full on the target. This command owns the structural
    refusals (see _validate_move), all BEFORE any write. Write order: documents
    UPDATE (project_id, root also parent_id+sort_key) → denormalized gather →
    live collab session patch → documents_moved_out/documents_moved_in emits.
    """
    db = await get_db()
    root = await fetch_one("documents", document_id)
    if not root or root.get("deleted_at") or not root.get("project_id"):
        raise HTTPException(status_code=404, detail="Document not found")
    source_project_id = root["project_id"]
    if target_project_id == source_project_id:
        raise HTTPException(
            status_code=400, detail="Document is already in the target project",
        )

    docs = await _collect_live_subtree(db, document_id)
    if not docs:
        raise HTTPException(status_code=404, detail="Document not found")
    parent_id = await _validate_move(db, docs, target_project_id, parent_id)

    await _rewrite_documents(db, docs, document_id, target_project_id, parent_id)

    document_ids = [did for did, d in docs.items() if not is_ref_row(d)]
    reference_ids = [did for did, d in docs.items() if is_ref_row(d)]
    all_ids = list(docs.keys())
    await _rewrite_denormalized(db, source_project_id, target_project_id, all_ids)

    # Live collab sessions cache project_id — patch or the next flush writes
    # doc_chunks under the OLD project and nudges the wrong project WS
    # (INVARIANT on CollabSession.project_id, collab/session.py).
    for did in all_ids:
        session = get_active_session("doc", did)
        if session is not None:
            session.project_id = target_project_id

    await _emit_move_events(
        source_project_id, target_project_id, document_ids, reference_ids,
    )
    return {
        "moved": len(all_ids),
        "document_ids": document_ids,
        "reference_ids": reference_ids,
    }


# ─── In-project move (reparent / reorder / kind conversion) ──────────────────


async def _load_move_target(document_id: str, project_id: str) -> dict:
    """Fetch the node to move: 404 missing/cross-project, 403 system document."""
    target = await fetch_one("documents", document_id)
    if not target or target.get("deleted_at"):
        raise HTTPException(
            status_code=404, detail="Target document not found",
        )
    if target.get("project_id") != project_id:
        # ARCH: uniform 404 for missing AND cross-project (no existence oracle).
        raise HTTPException(
            status_code=404, detail="Target document not found",
        )
    # References ARE movable — the blanket
    # "Reference documents cannot be moved" refusal is DELETED. The remaining guard is
    # NODE-shaped, not a reference branch: a reference carries a parent_id like every
    # other node (this is the SAME operation), so the same validation runs. Two facts
    # of the data model shape the edges, both properties of the node:
    #   - references are ordered in their OWN key space (is_reference=true group
    #     under the host) → they may be RE-HOSTED (landing at the top of the new
    #     host's ref group) but not TREE-reordered (after_id rejected — a ref is
    #     not part of its host's doc siblings; the documents reorder route is the
    #     ref reorder surface);
    #   - "project root" for a reference means the index document, not parent_id:null
    #     (a null-parent reference is listed by NO document's panel — a pre-existing
    #     defect tracked separately; this path must not mint it).
    if target.get("is_system"):
        raise HTTPException(status_code=403, detail="System documents cannot be moved")
    return target


async def _resolve_final_kind(
    target: dict, document_id: str, node_type: str | None,
) -> bool:
    """The node's kind after the move (True = reference), refusing conversions
    that cannot hold.

    node_type omitted = KEEP the kind. A stated kind equal to the current one
    is a plain move (no conversion edges fire). The four conversion refusals
    all run BEFORE any UPDATE — a refused conversion leaves the row untouched.
    """
    is_reference = is_ref_row(target)
    final_is_reference = is_reference if node_type is None else (
        node_type == "reference"
    )
    converting = final_is_reference != is_reference
    if converting and not final_is_reference and target.get("file_path"):
        # Bytes are not a tree document: the file part (audio bytes, an image)
        # exists only as a reference. Converting would mint a "document" whose
        # file part no panel renders.
        raise HTTPException(
            status_code=422,
            detail="A file-backed reference cannot become a tree document — "
                   "bytes are not a document; the text already lives in its body",
        )
    if converting and final_is_reference:
        db_check = await get_db()
        kids = await db_check.query(
            "SELECT VALUE id FROM documents "
            "WHERE parent_id = $id AND deleted_at IS NONE LIMIT 1",
            {"id": document_id},
        )
        if kids:
            # assert_parent_valid rejects a reference parent, and the schema
            # events (documents_parent_check) forbid it at the DB layer — the
            # children would be orphaned by the conversion. Refuse whole.
            raise HTTPException(
                status_code=422,
                detail="Cannot convert a document with children into a "
                       "reference — a reference cannot be a parent; move or "
                       "delete the children first",
            )
    return final_is_reference


def _check_reference_edges(
    final_is_reference: bool, new_parent: str | None, after_id: str | None,
) -> None:
    """Reference-specific edges (the one surviving fragment of the old guard, moved
    from "may not move" to "may not reorder" / "may not go root"). They bind the
    node's FINAL kind, so a conversion into a reference meets the same edges a
    born reference has."""
    if final_is_reference and not new_parent:
        raise HTTPException(
            status_code=400,
            detail="A reference cannot sit at the project root; host it on the "
                   "project's index document — it is then visible from every document",
        )
    if final_is_reference and after_id:
        raise HTTPException(
            status_code=400,
            detail="after_id orders tree documents only; an attached node is not "
                   "tree-ordered",
        )


async def _validate_destination(
    document_id: str, new_parent: str, project_id: str, scope_root: str | None,
) -> None:
    """Cycle guard + parent validity + scope wall for a non-root destination."""
    # Cycle guard: the new parent must not be the doc itself or one of its
    # descendants (moving a subtree into itself would orphan it). A reference has
    # no descendants, so this never trips for one — but it is harmless and keeps
    # the path uniform.
    await assert_no_cycle(document_id, new_parent)
    await _assert_not_into_memory(document_id, new_parent, project_id)
    # Reuse the single create/move parent invariant helper (404 missing/
    # cross-project, 400 reference parent). Also re-checks scope: a scoped key
    # may not move a doc to a parent outside its sandbox.
    await assert_parent_valid(new_parent, project_id)
    await require_doc_in_scope(scope_root, new_parent)


async def _assert_not_into_memory(
    document_id: str, new_parent: str, project_id: str,
) -> None:
    """400 when a document from outside the Memory subtree is moved into it."""
    memory_root = memory_folder_id(project_id)
    if memory_root not in await ancestor_chain(new_parent):
        return
    # INVARIANT: the Memory folder and its cards are not a re-parent destination
    # for a document that is not already memory; moves INSIDE memory stay legal.
    # Why: a plain document filed under Memory silently becomes memory the agent
    # retrieves, while the consolidation run's key is scoped to the Memory folder
    # and reorganizes cards within it.
    if memory_root in await ancestor_chain(document_id):
        return
    raise HTTPException(
        status_code=400,
        detail="A document cannot be moved into the Memory folder",
    )


async def write_reference_host(
    db, ref_id: str, project_id: str, new_parent: str | None,
) -> str:
    """The ONE reference host writer: set parent_id + is_reference and mint the
    TOP key of the new host's reference group. Returns the key.

    Every re-host (tree move / kind conversion here, the PATCH /api/references
    document_id branch) goes through this single writer, so a re-hosted ref
    always lands at the top of its new group with a live key in the ref key
    space. A CONVERSION overwrites the old tree key for the same reason — a
    kept tree key would leave the node in tree sibling queries and it would
    render twice (tree + panel).
    """
    new_key = await top_sibling_key(project_id, new_parent, is_reference=True)
    await db.query(
        "UPDATE type::record('documents', $id) "
        "SET parent_id = $v, is_reference = true, sort_key = $sk, "
        "updated_at = time::now()",
        {"id": ref_id, "v": new_parent, "sk": new_key},
    )
    return new_key


async def _write_tree_move(
    db, document_id: str, project_id: str,
    new_parent: str | None, after_id: str | None,
) -> str:
    """Tree document: compute the fractional sort_key for the landing spot within
    the NEW parent's sibling group via the shared place_after helper, then write
    parent + key. Returns the new key."""
    siblings = await sibling_rows(project_id, new_parent)
    new_key = place_after(
        siblings, document_id, after_id,
        not_sibling_detail="after_id is not a sibling under the target parent",
    )
    await db.query(
        "UPDATE type::record('documents', $id) "
        "SET parent_id = $v, sort_key = $sk, is_reference = false, "
        "updated_at = time::now()",
        {"id": document_id, "v": new_parent, "sk": new_key},
    )
    return new_key


async def _write_move(
    document_id: str, project_id: str, new_parent: str | None,
    after_id: str | None, final_is_reference: bool,
) -> str:
    """The single structural UPDATE; returns the new sort_key (a ref carries its
    new group's top key, so document_moved always names the landing position)."""
    db = await get_db()
    if final_is_reference:
        return await write_reference_host(db, document_id, project_id, new_parent)
    return await _write_tree_move(db, document_id, project_id, new_parent, after_id)


async def _emit_document_moved(
    target: dict, project_id: str, document_id: str, new_parent: str | None,
    old_parent: str | None, new_key: str | None, final_is_reference: bool,
) -> None:
    """The one `document_moved` broadcast of an in-project move."""
    # WHY: resolved off the module at call time (event_bus.emit), so a test
    # spying on event_bus.emit sees this broadcast.
    # Broadcast: identify BOTH the old and the new host, so a client showing
    # either can refresh its reference panel. The panel loads per-host
    # (loadReferences(project_id, document_id)); a move changes WHERE a node is
    # listed, not whether an existing ref:<id> anchor resolves (visibility is
    # inherited down the tree). previous_parent_id lets the OLD host recognize the
    # event concerns it — the tree-side document_moved carried only the new parent.
    # is_reference (the FINAL kind) + title let a second client move the node
    # between the tree and a references panel on a conversion — without them it
    # reparents blindly and keeps a phantom until reload.
    await event_bus.emit(
        "document_moved", project_id=project_id, document_id=document_id,
        parent_id=new_parent, previous_parent_id=old_parent, sort_key=new_key,
        is_reference=final_is_reference, title=target.get("title"),
    )


async def move_document_command(
    *, document_id: str, parent_id: str | None, after_id: str | None,
    project_id: str, scope_root: str | None = None, node_type: str | None = None,
) -> dict:
    """Reparent + reorder a node, and optionally CONVERT its kind (node_type).

    Access is the CALLER's gate (route require_document_full / the tool's
    _gate_move_target); this command owns fetch + system + scope + cycle +
    parent-validity + sibling ordering, then a single structural UPDATE +
    `document_moved` broadcast.
    parent_id None = project root for an unscoped key, the SCOPE root for a
    scoped one (scope.resolve_scoped_parent — resolved before the destination
    checks, so a null move cannot skip them); after_id None = top of the
    resulting sibling group.
    node_type None = KEEP the current kind (a plain move); "reference"/"document"
    convert the node in the same structural op — conversion is a MOVE, not a new
    tool ("take it and make it a reference" is one call, not a third node).
    """
    target = await _load_move_target(document_id, project_id)
    await require_doc_in_scope(scope_root, document_id)

    old_parent = extract_id(target.get("parent_id")) if target.get("parent_id") else None
    # INVARIANT(security): null/empty resolves to the KEY's root before the
    # reference-root check and the `if new_parent is not None:` destination block.
    # Why: the previous `parent_id or None` skipped destination validation for a
    # root move entirely — a scoped key could park its target OUTSIDE the wall (a
    # one-way escape: the next read 403'd). Resolving first means the cycle guard +
    # assert_parent_valid + require_doc_in_scope all run on a real in-scope id
    # (scope_root for a scoped key). Unscoped keys keep null ⇒ project root; the
    # `or None` tail keeps empty-string ⇒ project root for them.
    new_parent = resolve_scoped_parent(scope_root, parent_id) or None

    final_is_reference = await _resolve_final_kind(target, document_id, node_type)
    _check_reference_edges(final_is_reference, new_parent, after_id)
    if new_parent is not None:
        await _validate_destination(document_id, new_parent, project_id, scope_root)

    new_key = await _write_move(
        document_id, project_id, new_parent, after_id, final_is_reference,
    )
    await _emit_document_moved(
        target, project_id, document_id, new_parent, old_parent,
        new_key, final_is_reference,
    )
    return {
        "status": "applied", "document_id": document_id,
        "parent_id": new_parent, "sort_key": new_key,
        "is_reference": final_is_reference,
    }
