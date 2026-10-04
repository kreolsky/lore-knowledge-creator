"""Doc-command delete — single/batch soft-delete (subtree + lift modes).

Subsystem overview and ARCH notes live in documents/__init__.py.
See SYSTEM: documents (entry: backend/documents/__init__.py).
"""

import asyncio

from agent_config import PROTECTED_SYSTEM_ROLES
from cascade import _cascade_cleanup_document
from fastapi import HTTPException

import event_bus
from access import get_doc_project_id, require_project_full
from db import (
    DOC_BATCH_DELETE_COLUMNS,
    extract_id,
    fetch_one,
    get_db,
    validate_record_id,
)
from models import is_ref_row


def _is_protected_skeleton(doc: dict | None) -> bool:
    """A skeleton system doc (root + folders + Personas container) cannot be
    deleted. Why: deleting it soft-deletes the deterministic-id row but leaves it
    holding its path; the next agent turn calls ensure_agent_system_docs, whose
    bare CREATE then collides on the (project_id, path) UNIQUE index and raises —
    swallowed by the agent path as the generic 'Agent setup failed'. Personas, legacy
    leaf roles, and any plain doc remain deletable."""
    return bool(
        doc
        and doc.get("is_system")
        and doc.get("system_role") in PROTECTED_SYSTEM_ROLES
    )


async def _collect_subtree_ids(db, document_id: str) -> list[str]:
    """The target + all its live descendants (documents AND references).

    # ARCH: level-batched `parent_id IN $ids` walks bounded by the subtree size —
    # NOT db.get_descendant_ids's whole-project scan (that one materializes every
    # row of the project; the delete path must not pay for the rest of the tree).
    # Visited-set guard: a cyclic parent_id would otherwise loop the BFS. Why:
    # schema-level integrity is assumed, not enforced by Surreal, and an infinite
    # loop here would hang the delete endpoint. References are leaves (schema
    # forbids ref-as-parent) so they terminate naturally.
    """
    ids: list[str] = [document_id]
    seen: set[str] = {document_id}
    frontier: list[str] = [document_id]
    while frontier:
        rows = await db.query(
            "SELECT VALUE meta::id(id) FROM documents "
            "WHERE parent_id IN $ids AND deleted_at IS NONE",
            {"ids": frontier},
            idempotent=True,
        )
        nxt: list[str] = []
        for rid in (rows or []):
            if rid and rid not in seen:
                seen.add(rid)
                nxt.append(str(rid))
        ids.extend(nxt)
        frontier = nxt
    return ids


async def delete_document_command(document_id: str, *, delete_children: bool = True) -> dict:
    """Soft-delete a document in one of two modes:

    - delete_children=True (default): tombstone the target + ALL live descendants
      (docs AND refs) in place — no reparenting, nothing moves anywhere — and emit
      a single documents_deleted_batch event.
    - delete_children=False: legacy lift mode — tombstone the target, reparent its
      live non-reference children to the grandparent, emit per-doc events.

    Access gating lives route-side and splits by mode (see delete_document in
    routes/documents.py); this command stays pure.

    Concurrency WHY (gather over the shared SDK connection): the delete_document
    docstring in routes/documents.py.
    """
    project_id = await get_doc_project_id(document_id)
    db = await get_db()
    doc = await fetch_one("documents", document_id)
    # ARCH: protected skeleton (root + folders) cannot be deleted — see
    # _is_protected_skeleton. 403 (not 400) so the frontend can distinguish
    # "structurally protected" from a malformed request.
    if _is_protected_skeleton(doc):
        raise HTTPException(
            status_code=403,
            detail="This system document is protected and cannot be deleted.",
        )

    if delete_children:
        return await _delete_subtree(db, project_id, document_id)
    return await _delete_lift(db, project_id, document_id, doc)


async def _delete_lift(db, project_id: str | None, document_id: str, doc: dict | None) -> dict:
    """Lift-mode body of delete_document_command (see its docstring for mode)."""
    write_tasks: list = [
        db.query(
            "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
            {"id": document_id},
            idempotent=True,
        ),
    ]
    if doc:
        grandparent_id = doc.get("parent_id")
        write_tasks.append(db.query(
            "UPDATE documents SET parent_id = $gp, updated_at = time::now() "
            "WHERE parent_id = $did AND is_reference = false AND deleted_at IS NONE",
            {"gp": grandparent_id, "did": document_id},
            idempotent=True,
        ))

    cascade_task = asyncio.ensure_future(_cascade_cleanup_document(db, document_id))
    write_tasks.append(cascade_task)
    await asyncio.gather(*write_tasks)
    cascade_ref_ids = cascade_task.result()

    for rid in cascade_ref_ids:
        await event_bus.emit("reference_deleted", project_id=project_id, reference_id=rid)
    await event_bus.emit("entity_deleted", entity_type="doc", entity_id=document_id)
    await event_bus.emit("document_deleted", project_id=project_id, document_id=document_id)
    return {"success": True}


async def _tombstone_subtree(db, docs: dict[str, dict]) -> None:
    # Bulk tombstone (bound record refs, idempotent) + concurrent per-id cascade.
    # Post-tombstone each cleanup's own "live ref children" read returns empty —
    # harmless: every descendant id still gets its chunks/checkpoints/mentions/
    # chats/messages cleaned via the direct branches
    # (cascade is idempotent per cascade.py INVARIANT).
    write_tasks: list = [
        # WHY: a `WHERE id IN [list]` filter scans the whole table and its read set
        # conflicts with any concurrent write to that table (the embed worker's
        # per-reference status write then fails the delete after minutes of
        # scanning), so the tombstone targets records like cascade.py does.
        # A missing id is a no-op (pinned by test_documents_references.py).
        db.query(
            "UPDATE $rids.map(|$x| type::record('documents', $x)) "
            "SET deleted_at = time::now() WHERE deleted_at IS NONE",
            {"rids": list(docs.keys())},
            idempotent=True,
        ),
    ]
    write_tasks.extend(
        asyncio.ensure_future(_cascade_cleanup_document(db, did)) for did in docs
    )
    await asyncio.gather(*write_tasks)


async def _delete_subtree(db, project_id: str | None, document_id: str) -> dict:
    """Subtree-mode body of delete_document_command (see its docstring for mode)."""
    subtree_ids = await _collect_subtree_ids(db, document_id)
    docs = await _collect_live_docs(subtree_ids)
    if not docs:
        return {"success": True, "deleted": 0}
    # Defensive all-or-nothing: a protected skeleton anywhere in the subtree
    # rejects the delete. Structurally impossible via the API today (protected
    # folders sit directly under root) but cheap, and it matches the batch
    # endpoint's expanded-set parity.
    protected = [did for did, d in docs.items() if _is_protected_skeleton(d)]
    if protected:
        raise HTTPException(
            status_code=403,
            detail="One or more documents are protected system documents and cannot be deleted.",
        )

    await _tombstone_subtree(db, docs)

    # WHY: subtree mode emits exactly ONE documents_deleted_batch — never per-doc
    # entity_deleted/document_deleted/reference_deleted. Why: the batch subscriber
    # (collab/events.py) closes open collab/editor sessions for every id in
    # one sweep and the project WS broadcasts one message, so N per-doc events
    # would mean N round-trips and N tree mutations on every peer.
    await event_bus.emit(
        "documents_deleted_batch",
        project_id=project_id,
        document_ids=list(docs.keys()),
        reference_ids=[did for did, d in docs.items() if is_ref_row(d)],
    )
    return {"success": True, "deleted": len(docs)}


async def _collect_live_docs(document_ids: list[str]) -> dict[str, dict]:
    """Live (non-deleted) rows for the batch, keyed by id.

    # WHY the interpolated record-ref in-clause stays: reads take no conflicting
    # write locks, so this SELECT has none of the scan-class conflict the tombstone
    # UPDATE had; switching it to a $rids.map form would ripple for no benefit.
    """
    db = await get_db()
    in_clause = ",".join(
        f"type::record('documents','{validate_record_id(d)}')" for d in document_ids
    )
    rows = await db.query(
        f"SELECT {', '.join(DOC_BATCH_DELETE_COLUMNS)} FROM documents "
        f"WHERE id IN [{in_clause}] AND deleted_at IS NONE",
        idempotent=True,
    )
    docs: dict[str, dict] = {}
    for r in (rows or []):
        did = extract_id(r.get("id"))
        if did and r.get("project_id"):
            docs[did] = r
    return docs


async def _emit_batch_deleted(
    docs: dict[str, dict], cascade_tasks: dict, project_id: str,
    requested_ids: list[str],
) -> dict:
    """Emit the single batch event; return the response counts.

    # INVARIANT: reference_ids includes cascade-deleted descendant references, not just
    # directly-selected ones (parity with delete_document_command).
    # Why: descendant refs are reference_deleted only (never document_deleted), so the
    # frontend must drop their ghost reference entry / stale chat session via this set.
    # WHY: Single batch event replaces N×3 per-document emits (entity_deleted +
    # reference_deleted + document_deleted). The batch subscriber in collab/events.py
    # closes open editor sessions for every id; the project-WS subscription broadcasts
    # the batch to peers. Singular events are kept for the single-delete endpoint.
    """
    cascade_ref_ids = [rid for t in cascade_tasks.values() for rid in t.result()]
    reference_ids = list(dict.fromkeys(
        [did for did, d in docs.items() if is_ref_row(d)] + cascade_ref_ids
    ))
    await event_bus.emit(
        "documents_deleted_batch",
        project_id=project_id,
        document_ids=list(docs.keys()),
        reference_ids=reference_ids,
    )
    # `skipped` counts REQUEST ids that did not resolve to a tombstoned row. Why:
    # in subtree mode the deleted set is the expanded one (superset of the request),
    # so the former `total - len(docs)` would go negative.
    doc_id_set = set(docs.keys())
    skipped = len([d for d in requested_ids if d not in doc_id_set])
    return {"deleted": len(docs), "skipped": skipped}


async def _execute_batch_delete(
    db, docs: dict[str, dict], *, delete_children: bool = True,
) -> dict[str, asyncio.Future]:
    """Cascade-clean, bulk soft-delete and (lift mode only) reparent children.

    Lift mode (delete_children=False): reparent live children of every
    soft-deleted document to its grandparent. Subtree mode: no reparent statement
    anywhere — descendants are tombstoned together with their parent.
    Independent rows so we run them concurrently.
    """
    write_tasks: list = []
    cascade_tasks = {did: asyncio.ensure_future(_cascade_cleanup_document(db, did)) for did in docs}
    write_tasks.extend(cascade_tasks.values())
    # Bulk soft-delete in one statement (record-targeted — see _delete_subtree WHY).
    write_tasks.append(db.query(
        "UPDATE $rids.map(|$x| type::record('documents', $x)) "
        "SET deleted_at = time::now() WHERE deleted_at IS NONE",
        {"rids": list(docs.keys())},
        idempotent=True,
    ))
    if not delete_children:
        for did, d in docs.items():
            write_tasks.append(db.query(
                # WHY: only non-reference children are reparented; references are
                # cascade-deleted with the parent (parity with delete_document_command).
                # Why: reparenting a reference would steal it from the cascade's
                # `is_reference = true` read, leaking a ghost reference entry on the client.
                "UPDATE documents SET parent_id = $gp, updated_at = time::now() "
                "WHERE parent_id = $did AND is_reference = false AND deleted_at IS NONE",
                {"gp": d.get("parent_id"), "did": did},
                idempotent=True,
            ))
    await asyncio.gather(*write_tasks)
    return cascade_tasks


async def _expand_batch_to_subtrees(db, docs: dict[str, dict]) -> dict[str, dict]:
    """BFS-expand the collected live docs with their live descendants and
    re-collect rows for the expanded set.

    Why the re-collect: docs was built from the request ids and must be rebuilt
    so the bulk tombstone, the protected-skeleton 403 and
    _emit_batch_deleted cover the descendants. Deduped — a descendant also
    selected is fine, the tombstone is idempotent.
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for did in docs:
        for sid in await _collect_subtree_ids(db, did):
            if sid not in seen:
                seen.add(sid)
                expanded.append(sid)
    return await _collect_live_docs(expanded)


async def delete_documents_batch_command(
    document_ids: list[str], user: dict, *, delete_children: bool = True,
) -> dict:
    """Batch soft-delete multiple documents in one request.

    delete_children=True (default): every selected doc's live subtree is deleted
    with it — the id set is BFS-expanded and rows re-collected so the bulk
    tombstone, the protected-skeleton 403 and the batch event all cover the
    descendants (references batch-delete in routes/references.py passes nothing:
    refs are leaves, expansion is a no-op).

    HTTP-shape ARCH (POST-not-DELETE, IDOR scope): the delete_documents_batch
    docstring in routes/documents.py. The security guard itself is inline below
    — direct callers (references batch-delete) get it too.
    """
    db = await get_db()
    docs = await _collect_live_docs(document_ids)
    if not docs:
        return {"deleted": 0, "skipped": len(document_ids)}

    # INVARIANT(security): prevent IDOR via mixed-project arrays.  Why: a bulk op over a caller-supplied id array must not cross project boundaries — mixing projects would let a crafted request act on docs outside the caller's scope (IDOR); one project per batch is the scope guard.
    project_ids = {d["project_id"] for d in docs.values()}
    if len(project_ids) > 1:
        raise HTTPException(status_code=400, detail="All documents must belong to the same project")
    project_id = next(iter(project_ids))
    await require_project_full(project_id, user)

    if delete_children:
        docs = await _expand_batch_to_subtrees(db, docs)

    # ARCH: reject the whole batch if ANY member is a protected skeleton doc —
    # checked over the EXPANDED set, so a protected folder UNDER a deleted parent
    # rejects the batch instead of slipping through as an unselected descendant.
    # 403 (parity with single-delete). All-or-nothing so a partial batch never
    # leaves the caller thinking the protected doc went through.
    protected = [did for did, d in docs.items() if _is_protected_skeleton(d)]
    if protected:
        raise HTTPException(
            status_code=403,
            detail="One or more documents are protected system documents and cannot be deleted.",
        )

    cascade_tasks = await _execute_batch_delete(db, docs, delete_children=delete_children)
    return await _emit_batch_deleted(docs, cascade_tasks, project_id, document_ids)
