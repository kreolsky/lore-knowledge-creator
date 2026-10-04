"""Read-only agent tool executors (auto-run inside the loop / Tool-API).

# read_document execution + read-only dispatch (see SYSTEM: chat-agent-mode, agent/__init__.py).

read_document lives here; the search_materials and get_project_structure families
live in sibling modules (`search_exec` / `structure_exec`) and are re-exported below
(the public `*_tool` wrappers AND the internal `*_exec` helpers) so every existing
importer — Tool-API, MCP gateway, and the search unit tests — keeps its
`readonly_executors` import path. There is no `execute_readonly_tool` dispatcher:
`mode` coercion lives at the
dispatch site that needs it: mcp_gateway.dispatch.dispatch_tool).

This module also hosts the read_document executor core: the id-only target
resolve, the RBAC + scope gate chain, and the sidecar attach.
"""
import json
import logging
from dataclasses import dataclass

import settings
from fastapi import HTTPException

# Boundary-stability re-exports: external importers (Tool-API, MCP gateway) reach the
# public *_tool wrappers via this module's path, and the search unit tests reach
# _search_materials_exec directly — hence that one private helper is re-exported too.
# _get_project_structure_exec is NOT re-exported: its only caller was the deleted
# execute_readonly_tool; structure tests reach it via get_project_structure_tool.
import config
from access import get_document_access
from agent import doc_state
from agent.search_exec import (  # noqa: F401
    _search_materials_exec,
    search_materials_tool,
)
from agent.structure_exec import get_project_structure_tool  # noqa: F401
from db import extract_id, fetch_one
from models import is_ref_row

logger = logging.getLogger(__name__)


def _iso(value) -> str | None:
    """Serialize a SurrealDB datetime/value to an ISO string for JSON transport.

    SurrealDB 2.0 returns Python `datetime`; raw values fail json.dumps. Accepts
    datetime, ISO strings, or None. (backend.md — no precedent to copy.)"""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


@dataclass
class _ReadTarget:
    """A resolved, live read target: the doc row and the id every later gate
    runs on."""
    doc: dict
    resolved: str


async def _resolve_read_document_target(*, doc_id: str) -> _ReadTarget | dict:
    """The target resolve: fetch by id; on a miss/dead row build the not-found
    soft error (a dict — the caller returns it verbatim).

    Addressing is id-only — titles are not unique, so a name is not an address
    (`required: ["document_id"]` makes an invented address fail at validation
    BEFORE execution; ids come from search_materials / get_project_structure).
    """
    doc = await fetch_one("documents", doc_id) if doc_id else None
    if not doc or doc.get("deleted_at"):
        # No-silent-degradation: the miss is reported as an explicit soft
        # error the model can react to (search / re-list) instead of blind
        # retrying the same id.
        return {"error": "Document not found"}
    return _ReadTarget(doc=doc, resolved=doc_id)


async def _read_gate_error(
    *, resolved: str, doc: dict, project_id: str, user: dict,
    scope_root: str | None,
) -> str | None:
    """Same-project + per-doc RBAC + scope ceiling; the soft-error string, or
    None when every gate passed.

    # INVARIANT(security): access check is per-doc (get_document_access != None), NOT mere
    # project membership — otherwise a Viewer reads a private doc through the
    # agent. Why: project membership alone would let a Viewer read a private doc
    # via the agent; per-doc access (get_document_access) is the real gate, and
    # the doc must belong to the session's project (no cross-project reads).

    # INVARIANT: scope is a CEILING on top of RBAC, never a replacement.
    # Why: `require_doc_in_scope` runs AFTER get_document_access, so a scoped key
    # cannot read a doc outside its subtree even when RBAC would allow it.
    """
    if doc.get("project_id") != project_id:
        return "Document is not in this project"
    access = await get_document_access(resolved, user)
    if not access:
        return "No access to this document"
    # Scope ceiling (subtree-scoped agent keys): RBAC passed, now narrow to subtree.
    from scope import require_doc_in_scope

    try:
        await require_doc_in_scope(scope_root, resolved)
    except HTTPException as exc:
        # Map the scope 403 to the executor's soft-error shape (parity with the
        # access-denied branch) — the Tool-API/MCP callers translate to HTTP 403.
        # Narrows a blanket `except Exception` to HTTPException: the detail now comes
        # from the single producer (scope.out_of_scope_detail), so it is no longer
        # hand-copied here; and a NON-HTTP failure inside in_subtree→get_ancestor_ids
        # (a DB outage) propagates as a 500 via server.call_tool's catch-all instead of
        # being swallowed into a fake scope-403 — no silent degradation of an access
        # decision (a DB error must never masquerade as an access denial).
        return exc.detail
    return None


def _slice_read_window(
    full_content: str, offset: int, limit: int | None,
    *, slice_chars: int, max_chars: int,
) -> tuple[int, str]:
    """The read-path spill bound: a normalized code-point window `(start, content)`.

    # WHY: `content` is a code-point SLICE of the raw buffer — offset/limit
    # window, default AGENT_READ_SLICE_CHARS, hard-capped at
    # AGENT_READ_MAX_CHARS, bad windows normalized (offset<0→0,
    # offset>total→total, limit<=0→default; the class is normalized, not
    # branched per member — no error spends a turn).

    The bounds are REQUIRED arguments, resolved by the async caller through
    settings.get_all — the window never reads config itself, so no sync
    default leg can drift from the override-aware values.
    """
    total_chars = len(full_content)
    start = min(max(offset, 0), total_chars)
    window_limit = slice_chars if not limit or limit <= 0 else limit
    window_limit = min(window_limit, max_chars)
    return start, full_content[start:start + window_limit]


# INVARIANT (parity-by-read): `read_document.content` MUST be a byte-verbatim
# CONTIGUOUS code-point slice of `resolve_live_doc_state(doc_id)[0]` — and MUST
# equal the whole buffer whenever the response carries no `next_offset`. Why:
# divergence between the read tool's projection and the splice projection is
# the first-miss 409 root cause — the model copies `old_string` from this
# read and the resolver matches against the raw Y.Doc buffer; a GFM-expanded
# projection (table anchors → flat `| … |`, list-spacing normalization,
# escape processing) silently corrupted that copy in the no-live-session
# branch. A plain substring preserves byte-verity; any normalization destroys
# it.
# NOTE: chat context no longer carries bodies at all (scope pinning —
# routes.chat.context), so read_document is now the ONLY body path into the
# model, which makes this parity guarantee the single source of text truth.
def _build_read_result(
    doc: dict, resolved: str, full_content: str, *,
    offset: int, limit: int | None,
    slice_chars: int, max_chars: int,
) -> dict:
    """The base read payload: identity + the sliced `content`, self-describing.

    Truncation is self-describing (no silent degradation): `offset` +
    `total_chars` are ALWAYS returned; `next_offset` appears ONLY while content
    remains past the slice — absent means the whole document is in hand, so a
    model can never mistake a slice for the document's end.

    The file part is visible here — `media_type`, `has_file`,
    `processing_status`. `processing_status` is ambiguous between "no file" and
    "no derived text expected" (an image has a file AND a null status), so a
    SEPARATE `has_file` boolean answers "did my file arrive". Source `has_file`
    from the stored `file_path` — never surface the path itself (the download
    URL is get_file's job).
    """
    start, content = _slice_read_window(
        full_content, offset, limit, slice_chars=slice_chars, max_chars=max_chars,
    )
    result = {
        "doc_id": resolved,
        "title": doc.get("title") or "",
        "content": content,
        "offset": start,
        "total_chars": len(full_content),
        "is_reference": is_ref_row(doc),
        "parent_id": extract_id(doc.get("parent_id")) if doc.get("parent_id") else None,
        "created_at": _iso(doc.get("created_at")),
        "updated_at": _iso(doc.get("updated_at")),
        "media_type": doc.get("media_type"),
        "processing_status": doc.get("processing_status"),
        "has_file": bool(doc.get("file_path")),
        "is_index": bool(doc.get("is_index")),
        "is_system": bool(doc.get("is_system")),
    }
    if start + len(content) < len(full_content):
        # The ONLY "there is more" signal: absent ⇒ complete document in hand.
        result["next_offset"] = start + len(content)
    return result


async def _attach_read_sidecars(
    result: dict, *, resolved: str, full_content: str, tables: str,
    table_id: str | None,
) -> None:
    """Attach the tables/references siblings — everything that rides ALONGSIDE
    the content slice.

    # WHY: the sibling fields (`tables`, `references`) are returned on EVERY
    # page of a sliced read, identically — their presence never depends on
    # `offset`. Why: a field that appears only at offset 0 is indistinguishable
    # from "this document has no tables" on page 2, which is the same silent
    # degradation `next_offset` exists to prevent.
    # DEBT: the sidecars are recomputed per page — one references SELECT always,
    # plus a load_ydoc per page on a doc with table anchors and no live session,
    # and with tables="inline" every grid is repeated in full on every page.
    # Why deferred: the default mode is the cheap index, so the expensive shape
    # only occurs when the caller explicitly asks for inline grids, and no live
    # turn has yet paired inline with paging. Re-measure on real traffic; if the
    # pair shows up, the fix is to degrade inline→index for offset > 0 WITH an
    # explicit marker in the response (never a silent omission, per the INVARIANT
    # above).
    """
    # read_table is folded INTO this tool. `table_id` (if set) returns that one
    # table's full grid and overrides the index/inline mode; the mode otherwise
    # picks index-only (default, cheap — see the INVARIANT on _build_tables_field)
    # vs all grids (explicit). NOTE: built from FULL content (not the slice) — an
    # anchor cut by the window must not silently empty the index.
    if tables != "none":
        result["tables"] = await _build_tables_field(
            resolved, full_content, table_id=table_id, index_only=(tables == "index"),
        )
    # The node's attached leaf/file nodes (is_reference=true children), so an
    # agent sees what a `ref:<id>` anchor resolves to and can reuse an existing
    # image without a separate listing call. Always returned (not gated by tables).
    result["references"] = await _build_references_field(resolved)
    _warn_if_sidecars_outweigh_slice(resolved, result, result["content"])


async def _read_document_exec(
    *,
    doc_id: str,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    tables: str = "index",
    table_id: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Return the current content of a doc/ref as an offset/limit slice, gated by
    per-doc access. Phases: `_resolve_read_document_target` (target resolve) →
    `_read_gate_error` (RBAC + scope ceiling) → `resolve_live_doc_state` (raw
    buffer) → `_build_read_result` (parity-by-read slice) →
    `_attach_read_sidecars` (tables / references)."""
    target = await _resolve_read_document_target(doc_id=doc_id)
    if isinstance(target, dict):
        return target
    gate_error = await _read_gate_error(
        resolved=target.resolved, doc=target.doc, project_id=project_id,
        user=user, scope_root=scope_root,
    )
    if gate_error is not None:
        return {"error": gate_error}
    # Serve the SAME raw-buffer projection the edit resolver matches against,
    # so an `old_string` copied from a read is byte-verbatim by construction.
    # Reuse the shared helper — do NOT re-derive. (Contract: the parity-by-read
    # INVARIANT above _build_read_result.)
    full_content, _tables_json = await doc_state.resolve_live_doc_state(target.resolved)
    # The slice window is a config constant; the hard
    # ceiling stays a settings read.
    result = _build_read_result(
        target.doc, target.resolved, full_content, offset=offset, limit=limit,
        slice_chars=config.AGENT_READ_SLICE_CHARS,
        max_chars=await settings.get("AGENT_READ_MAX_CHARS"),
    )
    await _attach_read_sidecars(
        result, resolved=target.resolved, full_content=full_content, tables=tables,
        table_id=table_id,
    )
    return result


# Measuring the sidecars costs a serialization pass, so it runs only past a cheap
# precondition: full grids were requested, or the node carries an unusual number of
# attachments. The common path (index-only tables, a handful of references) never
# pays it.
_SIDECAR_REFERENCES_MEASURE_MIN = 20


def _warn_if_sidecars_outweigh_slice(doc_id: str, result: dict, content: str) -> None:
    """Log the one read shape that defeats the spill bound: sidecars heavier than
    the content slice they accompany.

    `content` is capped by AGENT_READ_SLICE_CHARS; `tables` and `references` are
    capped by NOTHING, so a document with fat grids can spend more of the model's
    context on the siblings than on the text — the bound then buys nothing. This
    is the live tripwire for the DEBT above (per-page sidecars); it observes and
    never alters the response.
    """
    tables_field = result.get("tables")
    references = result.get("references") or []
    grids_requested = any("rows" in entry for entry in tables_field or [])
    if not grids_requested and len(references) < _SIDECAR_REFERENCES_MEASURE_MIN:
        return
    sidecar_chars = len(json.dumps(
        {"tables": tables_field or [], "references": references}, ensure_ascii=False,
    ))
    if sidecar_chars <= len(content):
        return
    # WHY: the numbers go in the MESSAGE, not in `extra` — logging_conf's formatter
    # is "%(asctime)s %(name)s %(levelname)s %(message)s", so `extra` fields are
    # dropped and a tripwire without its measurements says nothing.
    logger.warning(
        "read_document sidecars outweigh the content slice: doc_id=%s "
        "sidecar_chars=%d content_chars=%d total_chars=%s truncated=%s "
        "n_tables=%d n_references=%d",
        doc_id, sidecar_chars, len(content), result.get("total_chars"),
        "next_offset" in result, len(tables_field or []), len(references),
    )


async def _build_references_field(doc_id: str) -> list[dict]:
    """The `references[]` sibling for read_document: a node's attached leaf/file
    nodes (documents with is_reference=true whose parent_id is this node).

    D6: this closes three holes at once — the
    terminal signal for the audio flow (poll processing_status), reusing an
    existing image (the `ref:` anchor resolves to a row here), and reading a doc
    that already embeds `![|WxH](ref:abc)`. `created_at` is carried because titles
    are NOT unique: a retried upload leaves several identically-named nodes, and
    without it an agent cannot tell which row is the one it just made.
    """
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT id, title, media_type, processing_status, file_path, created_at "
        "FROM documents WHERE parent_id = $host AND is_reference = true "
        "AND deleted_at IS NONE ORDER BY created_at DESC",
        {"host": doc_id},
    )
    out: list[dict] = []
    for r in rows:
        out.append({
            "document_id": extract_id(r.get("id")) if r.get("id") else None,
            "title": r.get("title") or "",
            "media_type": r.get("media_type"),
            "processing_status": r.get("processing_status"),
            "has_file": bool(r.get("file_path")),
            "created_at": _iso(r.get("created_at")),
        })
    return out


async def _build_tables_field(
    doc_id: str, content: str, *, table_id: str | None, index_only: bool,
) -> list[dict]:
    """Build the `tables` sibling field for read_document.

    Reuses the live tables Map resolution (session Y.Doc, else load_ydoc) +
    table_model_from_map + extract_table_labels — the SAME resolution path
    edit_table_cell matches against, so a read and a subsequent edit agree
    byte-for-byte. Does NOT reach for `_tables_json` (the discarded second return
    of resolve_live_doc_state): that is an incomplete projection.

    # WHY (carried from _read_table_exec): index_only=True (the default
    # read_document path) returns table_id + label + n_cols ONLY — no rows. Why: a
    # weak agent that gets every full grid dumped into context on a multi-table doc
    # burns its budget discovering one table; the index lets it pick one table to
    # fetch. A `table_id` argument (or index_only=False) returns that table's full
    # grid. see SYSTEM: mcp-gateway (both surfaces).
    """
    from table_serialize import extract_table_labels, table_model_from_map

    labels = extract_table_labels(content)

    # Short-circuit: no table anchors in content AND no explicit table_id requested
    # → there is nothing to index, so return [] WITHOUT resolving the live Y.Doc.
    # Why: the read_table fold made this run on EVERY read_document; without this
    # guard a tableless doc (the common case) paid a SECOND load_ydoc (2 DB
    # round-trips + snapshot replay) just to discover an empty tables Map. A doc
    # with anchors, or an explicit table_id, still resolves the Map (the genuine
    # tables case). labels is derived from content (already in hand), so this is
    # free and semantically exact — a table with no anchor is unreachable anyway.
    if not labels and table_id is None:
        return []

    from collab.registry import get_active_session
    from pycrdt import Map

    from ydoc_store import load as load_ydoc

    session = get_active_session("doc", doc_id)
    tables_root = session.ydoc.get("tables", type=Map) if session is not None else None
    if tables_root is None and session is None:
        live_doc = await load_ydoc(doc_id)
        tables_root = live_doc.get("tables", type=Map)

    tables: list[dict] = []
    ids = [table_id] if table_id is not None else (
        list(tables_root.keys()) if tables_root is not None else []
    )
    # A requested table_id always returns its full grid (index_only applies only to
    # the no-table_id index path); a missing table_id under index_only returns the
    # cheap index.
    per_table_full = (table_id is not None) or (not index_only)
    for tid in ids:
        if tables_root is None or tid not in tables_root:
            continue
        model_rows = table_model_from_map(tables_root[tid])
        n_cols = max((len(r) for r in model_rows), default=0)
        entry = {
            "table_id": tid,
            "label": labels.get(tid, ""),
            "n_cols": n_cols,
        }
        if per_table_full:
            entry["rows"] = [{"row": i, "cells": r} for i, r in enumerate(model_rows)]
        tables.append(entry)
    return tables


async def read_document_tool(
    *,
    doc_id: str,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    tables: str = "index",
    table_id: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Public read_document: content as an offset/limit slice + optional tables
    field, per-doc RBAC + same-project check."""
    return await _read_document_exec(
        doc_id=doc_id,
        project_id=project_id,
        user=user,
        scope_root=scope_root,
        tables=tables,
        table_id=table_id,
        offset=offset,
        limit=limit,
    )
