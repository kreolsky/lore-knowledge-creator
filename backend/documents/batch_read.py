"""Doc-command batch read — fetch_content_batch (one projected SELECT for N ids).

Subsystem overview and ARCH notes live in documents/__init__.py.
See SYSTEM: documents (entry: backend/documents/__init__.py).
"""

from fastapi import HTTPException

from access import require_project_read
from db import (
    DOC_TRANSCLUSION_COLUMNS,
    extract_id,
    get_db,
    serialize_record,
    validate_record_id,
)


def _bind_batch_ids(ids: list[str]) -> tuple[dict[str, str], list[str]]:
    """Param-bound per-id record refs.

    A bare `id IN $ids` with string ids matches nothing (the id column is a
    record ref). validate_record_id guards against malformed/table-injection
    ids before binding.
    """
    params: dict[str, str] = {}
    refs: list[str] = []
    for i, raw in enumerate(ids):
        try:
            validate_record_id(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid document id")
        params[f"id{i}"] = raw
        refs.append(f"type::record('documents', $id{i})")
    return params, refs


async def _collect_batch_rows(params: dict[str, str], refs: list[str]) -> dict[str, dict]:
    """Projected SELECT over the batch, keyed by record id.

    INVARIANT(schema-sync): this projected column list (DOC_TRANSCLUSION_COLUMNS) is
    hand-written SQL over the documents table — schema drift has NO compile-time signal.
    Why: the column list has no compile-time link to the table schema, so a rename/add
    silently drops a field the frontend mapping depends on; the schema-sync check
    (db._DOC_PROJECTIONS) is what surfaces the drift. It is checked alongside
    db.REF_META_COLUMNS / DOC_META_COLUMNS. content is projected (transclusion needs
    the body); ydoc_state is intentionally excluded (route docstring INVARIANT(corruption)).
    """
    db = await get_db()
    rows = await db.query(
        f"SELECT {', '.join(DOC_TRANSCLUSION_COLUMNS)} "
        f"FROM documents WHERE id IN [{', '.join(refs)}] AND deleted_at IS NONE",
        params,
    )
    by_id: dict[str, dict] = {}
    for r in (rows or []):
        did = extract_id(r.get("id"))
        if did:
            by_id[did] = r
    return by_id


def _merge_batch_items(
    by_id: dict[str, dict], ids: list[str], resolved_project: str,
) -> list[dict]:
    """Serialize + live-merge the in-project rows, preserving REQUEST order.

    Live-merge contract: routes/documents.py get_documents_batch docstring
    (INVARIANT(liveness)).
    """
    from collab.events import merge_live_content

    items: list[dict] = []
    for raw in ids:
        row = by_id.get(raw)
        if not row or (row.get("project_id") or None) != resolved_project:
            continue  # missing/deleted, or cross-project (filtered, not 404'd)
        serialized = serialize_record(row, "document_id")
        serialized = merge_live_content(serialized, raw)
        items.append(serialized)
    return items


async def fetch_content_batch(ids: list[str], user: dict) -> dict:
    """Batch-fetch content for N document/reference ids in ONE projected SELECT.

    Full HTTP-contract prose (access model, uniform-404 gate, live-merge and
    projection invariants): the get_documents_batch docstring in
    routes/documents.py — the route owns the contract text.
    """
    if not ids:
        return {"items": []}

    params, refs = _bind_batch_ids(ids)
    by_id = await _collect_batch_rows(params, refs)

    # Deterministic project resolution: first REQUEST id that exists sets the scope.
    resolved_project: str | None = None
    for raw in ids:
        row = by_id.get(raw)
        if row:
            resolved_project = row.get("project_id") or None
            break

    if resolved_project is None:
        # All ids missing/unknown/deleted -> 404 (INVARIANT security: collapses
        # "all missing" into the same code as "no access", matching the single-doc
        # uniform-404 gate so no existence/access oracle leaks).
        raise HTTPException(status_code=404, detail="Documents not found")

    await require_project_read(resolved_project, user)
    return {"items": _merge_batch_items(by_id, ids, resolved_project)}
