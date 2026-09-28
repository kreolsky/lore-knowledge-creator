"""Tool-API mutating edit tools — edit_document + edit_table_cell + append_to_document.

# ARCH: each mutating tool splits into an `auto` path (apply directly through the
# shared CRDT primitives in _common) and a `confirm` path (refuse with 409
# confirmation_required — the driver's dsh approval ask is the confirmation;
# a system-doc target is the one other confirm). Apply-mode resolution is unified
# via _resolve_apply_or_force.
# Import DAG: imports _router + _common only; reaches scope/selection_conflict/
# tool_api_surface/edit_primitives lazily inside the handlers.
"""
import settings
from agent.context import get_agent_context
from fastapi import Depends, HTTPException
from textmatch import _section_end_offset

from config import AGENT_EDIT_MAX_CHARS
from models import (
    ToolAddTableColumn,
    ToolAddTableRows,
    ToolAppendDocument,
    ToolCreateTable,
    ToolEditDocument,
    ToolEditTableCell,
)
from routes.tool_api._common import (
    _apply_add_table_column_direct,
    _apply_add_table_rows_direct,
    _apply_create_table_direct,
    _apply_edit_table_cell_direct,
    _apply_edits_direct,
    _body_to_edits,
    _body_to_table_edits,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool
from scope import gate_mutation_target


@track_agent_tool("edit_document")
async def tool_edit_document(
    body: ToolEditDocument, ctx: dict = Depends(get_agent_context),
):
    user = ctx["user"]
    # M7: shared fetch+RBAC+scope gate (was inlined at ~6 sites). Gating here at the
    # route rejects out-of-scope targets before any content is resolved for BOTH the
    # auto path (re-checked in apply_edits_to_document as defense-in-depth) and the
    # confirm path — closes a substring-existence oracle on confirm.
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )

    # Normalize the body to an edits list (advertised `edits[]` OR legacy shim).
    edits = _body_to_edits(body)

    # Apply-mode resolution (PR4 R2): unified single source, with the MCP
    # force-auto override (see _resolve_apply_or_force).
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    if decision.mode != "auto":
        # Confirm → refuse with the ask signal (the confirm preference or a
        # system-doc target): the driver asks through dsh's approval service and
        # retries with the verdict marker; a reject returns as the call's own
        # result so the model replans inside the same turn.
        _refuse_unconfirmable("edit_document")

    # Advisory region-lock pre-check: reject if ANY edit's region
    # intersects another participant's active selection. NON-atomic by design —
    # it races the CRDT write; the authoritative convergence line stays the
    # apply path's re-resolve. Excludes the acting user's own selections.
    from routes.chat.selection_conflict import (
        selection_conflict_check,
        selection_conflict_response,
    )

    for e in edits:
        conflict = await selection_conflict_check(
            body.document_id, e["old_string"], exclude_user_id=ctx["user_id"],
        )
        if conflict is not None:
            raise HTTPException(status_code=409, detail=selection_conflict_response(conflict))
    return await _apply_edits_direct(
        doc_id=body.document_id, edits=edits,
        project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"), region=body.region,
    )


@track_agent_tool("edit_table_cell")
async def tool_edit_table_cell(
    body: ToolEditTableCell, ctx: dict = Depends(get_agent_context),
):
    """Pointwise cell edit(s) for an editable table block (§ agent-table-read-and-cell-edit).

    Batch-capable (edits: [...]); a single cell is a list of one. Mirrors
    `tool_edit_document`'s apply-mode split, minus region-lock (table cells are not
    covered by the text-region advisory lock).
    """
    user = ctx["user"]
    # M7: shared fetch+RBAC+scope gate (gates both apply modes at the route).
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )

    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    edits = _body_to_table_edits(body)
    if decision.mode != "auto":
        _refuse_unconfirmable("edit_table_cell")
    return await _apply_edit_table_cell_direct(
        document_id=body.document_id, edits=edits,
        project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )


# ─── append_to_document ───────────────────────────────────────────────────────
# ARCH: append is NOT a new write path. It resolves
# an insertion offset (end of doc, or end of one heading section), synthesizes ONE
# str_replace edit (a unique tail anchor → anchor + appended text), and runs it
# through the EXISTING edit CRDT executor (_apply_edits_direct). An empty document
# has no anchor, so it takes the content-set path (route_document_content) — same
# convergence core, no ydoc desync. The edit core re-resolves old_string against
# live content, so a tail that drifted since the read surfaces as a normal 409.


async def _build_append_edit(
    content: str, append_text: str, section: str | None = None,
) -> dict | None:
    """Synthesize ONE str_replace edit that appends `append_text` at the end of
    the document (or of `section`). Returns {old_string, new_string} or None.

    None means the document head is empty/whitespace-only → the caller takes the
    content-set path (no str_replace anchor exists for an empty doc). Raises
    LookupError if `section` names no heading.

    # WHY: old_string is a VERBATIM unique suffix of the head that resolves
    # EXACTLY at the insertion offset (verified via the shared edit-range resolver),
    # so the replacement appends in place and stays under the full-rewrite fraction.  Why: anchoring the insert at a verbatim unique suffix resolved via the shared resolver guarantees it lands at the right offset and the net change stays under the full-rewrite fraction (no wholesale replacement).
    # Why: a non-unique or misplaced anchor would insert the text in the wrong spot
    # or trip the edit core's ambiguity/full-rewrite guards.
    """
    full_rewrite_fraction = await settings.get("AGENT_FULL_REWRITE_FRACTION")
    insert_at = _section_end_offset(content, section) if section else len(content)
    head = content[:insert_at]
    if not head.strip():
        return None  # empty document → content-set path
    append_clean = append_text.lstrip("\n")
    if not append_clean:
        return None  # nothing meaningful to append
    # Exactly one blank line between the head's last content and the appended block,
    # regardless of how many trailing newlines the head already has.
    trailing_nl = len(head) - len(head.rstrip("\n"))
    sep = "\n" * max(0, 2 - trailing_nl)
    # Shortest unique tail anchor ending exactly at insert_at. Capped under the
    # full-rewrite fraction; a repetitive tail with no unique short anchor raises
    # (the agent should fall back to edit_document with explicit context).
    max_len = min(len(head), max(1, int(full_rewrite_fraction * len(content)) - 1))
    from agent.tool_api_surface import resolve_edit_range_for_gate

    for length in range(1, max_len + 1):
        anchor = head[-length:]
        rng = resolve_edit_range_for_gate(
            content, anchor, full_rewrite_fraction=full_rewrite_fraction)
        if isinstance(rng, tuple) and rng[1] == insert_at:
            return {"old_string": anchor, "new_string": anchor + sep + append_clean}
    raise HTTPException(
        status_code=409,
        detail=(
            "Could not anchor an append — the document tail is not unique enough. "
            "Use edit_document with an explicit old_string instead."
        ),
    )


async def _apply_append_direct(
    *, doc_id: str, append_text: str, section: str | None,
    project_id: str, user: dict, scope_root: str | None = None, region=None,
) -> dict:
    """Apply an append directly through the live CRDT path (auto / external-apply path).

    Self-contained: fetch + access + scope + oversize, then synthesize the edit
    from the live Y.Doc content and run it through the shared edit executor. An
    empty document takes the content-set path (route_document_content) — the ONE
    convergence core for content mutation, so ydoc never desyncs.
    """
    # M7: shared fetch+RBAC+scope gate.
    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )
    if len(append_text) > AGENT_EDIT_MAX_CHARS:
        raise HTTPException(status_code=413, detail="Appended text too large")

    # Guard the AUTHORED append
    # text here, next to the size cap. Why here and not in route_document_content:
    # the empty-doc branch below converges the WHOLE buffer via route_document_content
    # (which no longer scans — a buffer may carry an image a human typed), so the
    # remote-image rejection must land on the text the agent actually authored, at
    # BOTH the empty-doc (content-set) and non-empty (synthesized-edit) branches.
    # This is the empty-doc branch's guard; the non-empty branch is covered by
    # route_document_edits' scan over the synthesized edit's new_text.
    from agent.doc_state import reject_remote_images

    reject_remote_images(append_text)

    from agent.doc_state import resolve_live_doc_state

    content, _ = await resolve_live_doc_state(doc_id)
    try:
        edit = await _build_append_edit(content, append_text, section)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if edit is None:
        return await _apply_append_to_empty_doc(
            doc_id=doc_id, append_text=append_text, project_id=project_id, user=user,
        )

    # Non-empty: run the synthesized edit through the EXISTING batch executor (it
    # re-resolves old_string against live content under the per-doc lock).
    return await _apply_edits_direct(
        doc_id=doc_id, edits=[edit], project_id=project_id, user=user,
        scope_root=scope_root, region=region,
    )


async def _apply_append_to_empty_doc(
    *, doc_id: str, append_text: str, project_id: str, user: dict,
) -> dict:
    """Set content on an empty document via the convergence core (route_document_content
    — the ONE convergence core for content mutation, same path the edit tail uses, so
    ydoc never desyncs). No checkpoint: an empty doc has no prior state to revert to."""
    from agent.collab_writes import broadcast_agent_presence
    from agent.doc_state import route_document_content
    from markdown_normalize import normalize_list_spacing, unwrap_placeholder_brackets

    # Unwrap `(<id>)` placeholder-bracket destinations — one of the three agent
    # write sites for the unwrap (append into an EMPTY document, the one append
    # branch that bypasses the edit executor and normalizes on its own).
    await route_document_content(
        doc_id=doc_id,
        new_content=unwrap_placeholder_brackets(normalize_list_spacing(append_text)),
        project_id=project_id,
    )
    await broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "doc_id": doc_id}


@track_agent_tool("append_to_document")
async def tool_append_document(
    body: ToolAppendDocument, ctx: dict = Depends(get_agent_context),
):
    """Append markdown at the end of a document (or of one heading section).

    Splits into the same auto/confirm modes as edit_document. The confirm path
    stores the raw append intent (document_id/content/section); apply re-resolves
    the insertion point against live content at apply time.
    """
    user = ctx["user"]
    # M7: shared fetch+RBAC+scope gate.
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )

    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("append_to_document")
    return await _apply_append_direct(
        doc_id=body.document_id, append_text=body.content,
        section=body.section, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"), region=body.region,
    )


# ─── create_table / add_table_rows / add_table_column ─────────────────────────
# ARCH: structural table tools sharing the same auto/confirm
# split as the other mutating tools. Each auto path delegates to the shared
# standalone applier in _common; each confirm path creates ONE proposal carrying
# the op payload (applied via the chat `_apply_table_op_proposal` or the Tool-API
# `proposals.py` apply dispatch).


@track_agent_tool("create_table")
async def tool_create_table(
    body: ToolCreateTable, ctx: dict = Depends(get_agent_context),
):
    """Append a NEW editable table block (header + initial rows) + its anchor."""
    user = ctx["user"]
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("create_table")
    return await _apply_create_table_direct(
        document_id=body.document_id, label=body.label, rows=body.rows,
        section=body.section, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )


@track_agent_tool("add_table_rows")
async def tool_add_table_rows(
    body: ToolAddTableRows, ctx: dict = Depends(get_agent_context),
):
    """Append N rows to the bottom of an existing table."""
    user = ctx["user"]
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("add_table_rows")
    return await _apply_add_table_rows_direct(
        document_id=body.document_id, table_id=body.table_id, rows=body.rows,
        project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )


@track_agent_tool("add_table_column")
async def tool_add_table_column(
    body: ToolAddTableColumn, ctx: dict = Depends(get_agent_context),
):
    """Insert ONE column into an existing table."""
    user = ctx["user"]
    target, _ = await gate_mutation_target(
        user=user, doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=bool(target.get("is_system")),
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("add_table_column")
    return await _apply_add_table_column_direct(
        document_id=body.document_id, table_id=body.table_id,
        at_index=body.at_index, header=body.header, values=body.values,
        project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )
