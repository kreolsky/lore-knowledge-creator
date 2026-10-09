"""Tool-API — the permission boundary every tool call passes; ours by design, independent of the brain.

This is the SPINE module of the routes/tool_api/ package: the
write-mode resolver, the standalone apply primitives, and the confirmation
refusal shared by every domain module. The per-tool handlers live in reads/edits/creates;
routes/tool_api/__init__.py wires them onto the shared router.

# SYSTEM: tool-api — the permission boundary every tool call passes; ours by
# design, independent of the brain.
# ARCH: this surface is owned by us, independent of the brain. It maps 1:1 to existing backend services — NO new business logic. Reads reuse
# the agent read-only executors; writes funnel through the SAME live-CRDT path
# (apply_external_content_change → set_content + ydoc: backplane) + pre-edit
# checkpoint that the in-editor agent uses. Two write behaviors:
#   - auto   → apply immediately through the CRDT path + checkpoint (AS mode);
#   - confirm→ REFUSE with 409 {code: confirmation_required}: the mid-turn
#              approval ask lives in the DRIVER (dsh's user-approval service),
#              so a call this surface cannot confirm tells the caller to ask —
#              the Lore driver retries with the X-Agent-Verdict marker after
#              dsh's approval round; a caller that cannot ask (an external
#              brain without the driver) fails closed here.
#
# Auth: the agent-key → owning-user contract lives with get_agent_context in
# agent/context.py.

# ARCH (import DAG): _common is a pure LEAF — it imports only stdlib + deps/db/models
# + routes.tool_api_telemetry at module level, and reaches the agent
# library (apply_policy) and the chat package (tool_api_surface /
# selection_conflict) LAZILY inside
# functions. Domain modules import ONLY _common + _router; no domain imports another
# domain. Nothing in routes.chat.* imports routes.tool_api back → no cycle.
"""
from fastapi import HTTPException

from access import get_project_access
from models import ToolEditDocument, ToolEditTableCell

# ─── Write-mode resolution ────────────────────────────────────────────────────
# ARCH: apply-mode resolution is unified in agent.apply_policy.
# The per-call endpoints below call `resolve_apply_mode` from there; the
# duplicated local resolvers are deleted.
#
# The _resolve_apply_or_force cells:
# - WHY (the marker): dsh's user-approval service already asked the user for
#   THIS call and the answer was allowed-once (the audit pair sits on the
#   driver's session log) — the approved call must apply, not re-enter the
#   confirm path. A system-doc target applies too: the marker reaches ctx
#   ONLY on a driver-attested request (agent.context.driver_attested drops it
#   otherwise), so it is the user's own approval of this exact call. Refusing
#   it made the agent self-edit path (Knowledge, rules, skills) a dead end —
#   the card asked, the user allowed, the call still 403'd.
# - WHY (MCP): the MCP surface has no decision card and no verdict flow; the
#   subtree scope + read-write key IS the trust boundary and every write is
#   reversible via History. A system-doc target is the one exception: REJECTED
#   (403), never force-applied — the agent's own safety config (rules/skills)
#   is not editable over MCP.
# - WHY (the access-level cell): RBAC at dispatch (gate_mutation_target 403s a
#   non-full caller) is the authority, so `confirm` means the call needs the
#   user's approval BEFORE it applies, and this surface no longer holds it
#   (step 7).

# ARCH: consent is taken ONCE, where a run is started — not per call inside it.
# The user approves launching the pipeline / trigger / subagent; every call that
# run then makes is the responsibility of whoever launched it, bounded only by the
# launcher's RBAC and the key's subtree wall. A key carried by a background run
# therefore does NOT need a read-only or per-call permission axis, and `auto_apply`
# is deliberately not read on this surface (it is the MCP surface's binary grant).
# Why: Lore is self-hosted for enthusiasts and small/medium teams, not a multi-
# tenant service — a mechanism priced for a hostile pack author is out of the
# product by decision, and re-deriving it per call turns one human decision into a
# stream of them. Do not add a permission gate here; narrow the run instead
# (pack set, brief, scope_root).


async def _resolve_apply_or_force(
    ctx: dict, *, ui_preference: str, is_system: bool,
):
    """One decision: the approval marker / MCP override, else the fallback table."""
    from agent.apply_policy import (
        AUTO,
        ApplyDecision,
        resolve_apply_mode,
    )

    if ctx.get("verdict") == "allowed-once":
        return ApplyDecision(AUTO, "verdict_approved")
    if ctx.get("force_auto_apply") is True:
        if is_system:
            raise HTTPException(
                status_code=403,
                detail="System documents are not editable over the MCP surface",
            )
        return ApplyDecision(AUTO, "mcp_force_auto")
    return resolve_apply_mode(is_system=is_system, ui_preference=ui_preference)


def _refuse_unconfirmable(tool_name: str):
    """The ONE confirm-cell refusal: a mutating call resolved to `confirm`
    cannot apply without the user's approval, and the mid-turn ask lives in
    the DRIVER (dsh's user-approval service) — not on this surface (step 7
    deleted the backend hold).

    The Lore driver treats this 409 + code as the ASK signal: it parks the
    call on dsh's approval service and retries with the X-Agent-Verdict marker
    once the user allows. A caller that cannot ask (an external brain with no
    driver) fails closed here — never a silent apply.

    # INVARIANT(security): a call that resolves to `confirm` and arrives
    # without approval is REFUSED — never auto-applied.
    # Why: the confirm cell is either the user's explicit confirm preference
    # or a system-doc target (the agent self-edit privilege path), so
    # degrading an unconfirmable call to an apply would hand the agent exactly
    # what the table exists to protect.
    """
    raise HTTPException(
        status_code=409,
        detail={
            "code": "confirmation_required",
            "detail": (
                f"{tool_name} needs the user's approval before it applies. The "
                "Lore driver asks through the mid-turn approval card and "
                "retries; otherwise ask the user to make this change themselves."
            ),
        },
    )


# ─── Standalone apply primitives ──────────────────────────────────────────────
# ARCH: the edit-apply path lives in agent.tool_api_surface.apply_edit_to_document (the
# single home for the CRDT convergence primitives) — reused, not duplicated. Create
# paths reuse the shared documents factory / reference shape directly.


async def _apply_edits_direct(
    *, doc_id: str, edits: list[dict], project_id: str, user: dict,
    scope_root: str | None = None, region=None,
) -> dict:
    """Delegate a BATCH of edits to the shared standalone batch applier (plan
    "quizzical-mixing-marble"). One atomic call: all-or-nothing, one checkpoint,
    one Y.Doc publish."""
    from agent.tool_api_surface import apply_edits_to_document
    return await apply_edits_to_document(
        doc_id=doc_id, edits=edits,
        project_id=project_id, user=user, scope_root=scope_root, region=region,
    )


def _body_to_edits(body: ToolEditDocument) -> list[dict]:
    """Normalize an edit_document body to an edits list (advertised `edits[]` OR
    the legacy singular old_string/new_string shim, coalesced to one element)."""
    if body.edits is not None:
        return [{"old_string": e.old_string, "new_string": e.new_string} for e in body.edits]
    return [{"old_string": body.old_string or "", "new_string": body.new_string or ""}]


def _body_to_table_edits(body: ToolEditTableCell) -> list[dict]:
    """Normalize an edit_table_cell body to an edits list (advertised ``edits[]`` OR
    the legacy singular {table_id,row,column,...} shim, coalesced to one element). The
    shared batch applier always sees the batch shape (single cell = list of one)."""
    if body.edits is not None:
        return [
            {"table_id": e.table_id, "row": e.row, "col": e.col,
             "column": e.column, "old_value": e.old_value, "new_value": e.new_value}
            for e in body.edits
        ]
    return [{
        "table_id": body.table_id, "row": body.row if body.row is not None else 0,
        "col": body.col, "column": body.column,
        "old_value": body.old_value or "", "new_value": body.new_value or "",
    }]


async def _apply_create_document_direct(
    *, title: str, content: str, parent_id: str | None, project_id: str, user: dict,
    scope_root: str | None = None,
) -> dict:
    """Create a document via the shared primitive (sort_key invariant preserved).

    PR4 R3: the create body lives in ONE place —
    `agent.tool_api_surface.create_document_via_collab`. The access
    check stays here (the Tool-API direct path uses project access).
    """
    access = await get_project_access(project_id, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Full project access required to create")
    from agent.collab_writes import create_document_via_collab

    created = await create_document_via_collab(
        title=title, content=content, parent_id=parent_id,
        project_id=project_id, user=user, scope_root=scope_root,
    )
    return {"status": "applied", "doc_id": created["doc_id"]}


async def _apply_create_reference_direct(
    *, document_id: str, title: str, content: str, media_type: str,
    source_url: str | None, project_id: str, user: dict,
    scope_root: str | None = None, author_name: str | None = None,
) -> dict:
    """Create a reference (is_reference=true document).

    `author_name` (S1): the DISPLAY-axis byline — the making agent key's label
    (api_key_auth.agent_author_name at the surface). None keeps the user's name.
    """
    access = await get_project_access(project_id, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Full project access required to create")
    from agent.collab_writes import create_reference_via_collab

    created = await create_reference_via_collab(
        document_id=document_id, title=title, content=content,
        media_type=media_type, source_url=source_url,
        project_id=project_id, user=user, scope_root=scope_root,
        author_name=author_name,
    )
    ref_id = created["doc_id"]
    return {"status": "applied", "reference_id": ref_id, "doc_id": ref_id}


async def _apply_edit_table_cell_direct(
    *, document_id: str, edits: list[dict], project_id: str, user: dict,
    scope_root: str | None = None,
) -> dict:
    """Delegate a BATCH of table-cell edits to the shared standalone applier. Each edit
    dict carries {table_id, row, col, column, old_value, new_value}; ``document_id`` is
    threaded onto each (the applier expects it per-edit). Single cell = list of one."""
    from agent.table_writes import apply_edit_table_cell
    return await apply_edit_table_cell(
        edits=[{"document_id": document_id, **e} for e in edits],
        project_id=project_id, user=user, scope_root=scope_root,
    )


async def _apply_add_table_rows_direct(
    *, document_id: str, table_id: str, rows: list[list[str]],
    project_id: str, user: dict, scope_root: str | None = None,
) -> dict:
    """Delegate an add_table_rows to the shared standalone applier."""
    from agent.table_writes import apply_add_table_rows
    return await apply_add_table_rows(
        doc_id=document_id, table_id=table_id, rows_matrix=rows,
        project_id=project_id, user=user, scope_root=scope_root,
    )


async def _apply_add_table_column_direct(
    *, document_id: str, table_id: str, at_index: int | None,
    header: str | None, values: list[str] | None,
    project_id: str, user: dict, scope_root: str | None = None,
) -> dict:
    """Delegate an add_table_column to the shared standalone applier.

    ``at_index=None`` resolves to the END (``len(columns)``) inside the applier after
    it reads the live column count — matching frontend ``addColumn``'s default."""
    from agent.table_writes import apply_add_table_column
    return await apply_add_table_column(
        doc_id=document_id, table_id=table_id, at_index=at_index,
        header=header, values=values,
        project_id=project_id, user=user, scope_root=scope_root,
    )


async def _apply_create_table_direct(
    *, document_id: str, label: str | None, rows: list[list[str]],
    section: str | None, project_id: str, user: dict, scope_root: str | None = None,
) -> dict:
    """Delegate a create_table to the shared standalone applier."""
    from agent.table_writes import apply_create_table
    return await apply_create_table(
        doc_id=document_id, label=label, rows_matrix=rows, section=section,
        project_id=project_id, user=user, scope_root=scope_root,
    )
