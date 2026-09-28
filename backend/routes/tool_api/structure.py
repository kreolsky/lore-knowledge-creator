"""Tool-API structural mutating tools — move_document + rename_document.

# ARCH: move is a STRUCTURAL executor, not a content
# mutation. It reparents a tree document and/or reorders it within a sibling group
# through the ONE in-project move (documents.move.move_document_command, shared with
# the PATCH parent_id branch). It NEVER touches the CRDT text, so it is
# NOT reversible via the Lore History panel (stated verbatim in the tool description)
# — reversibility is via re-moving the document.
#
# rename is the SURFACE over the rename core (documents.service.rename_document,
# plan rename-core-one-path): it adds no rename logic, only the agent-tool shape —
# the shared structural gate, apply resolution, and the mid-turn hold. Like move it
# is structural (a title is metadata, not buffer text), so it is likewise NOT
# reversible via the History panel — undo by renaming back.
#
# parent_id semantics (Variant A): a value = new parent; null = project root —
# for an UNSCOPED key. Under a subtree-scoped key null/empty resolves to the
# SCOPE root (scope.resolve_scoped_parent, the same normalization create uses) —
# a null move may not escape the key's sandbox.
# after_id: a sibling under the resulting parent to land after; null = top of group.
#
# Import DAG: imports documents.move + _common; reaches documents.service / scope
# lazily inside the handlers.
"""
from agent.context import get_agent_context
from documents.move import move_document_command
from fastapi import Depends, HTTPException

from access import get_document_access
from db import fetch_one
from models import ToolMoveDocument, ToolRenameDocument
from routes.tool_api._common import (
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool


async def _gate_move_target(
    *, document_id: str, project_id: str, user: dict, scope_root: str | None,
    verb: str, past: str,
) -> tuple[dict, str]:
    """Fetch + 404 + system + access + scope gate for a STRUCTURAL tool target
    (move / rename) — the validation-time twin of move_document_command's own
    re-check (same order, same details). Access lives HERE only: the command
    is shared with the PATCH route, whose own guard is require_document_full. `verb` / `past` parameterize the 403 text ("move"/"moved",
    "rename"/"renamed") so each tool's refusal names its own operation. Two
    words, not one plus a suffix: English past tense is not a concatenation
    ("copy" would render "copyd"). Both callers state both.

    NOT scope.gate_mutation_target: the 403/404 details are tool-specific
    ("…required to move") and a memory fact IS movable-by-idle-today — folding
    the two gates would change refusal text and add the memory rejection.
    """
    target = await fetch_one("documents", document_id)
    if not target or target.get("deleted_at"):
        raise HTTPException(
            status_code=404, detail="Target document not found",
        )
    if target.get("project_id") != project_id:
        # Uniform 404 for missing AND cross-project (no existence oracle —
        # same policy as scope.gate_mutation_target).
        raise HTTPException(
            status_code=404, detail="Target document not found",
        )
    if target.get("is_system"):
        raise HTTPException(
            status_code=403, detail=f"System documents cannot be {past}",
        )
    access = await get_document_access(document_id, user)
    if access != "full":
        raise HTTPException(
            status_code=403, detail=f"Full access required to {verb}",
        )
    from scope import require_doc_in_scope

    await require_doc_in_scope(scope_root, document_id)
    return target, access


@track_agent_tool("move_document")
async def tool_move_document(
    body: ToolMoveDocument, ctx: dict = Depends(get_agent_context),
):
    """Reparent and/or reorder a document. NOT reversible via the Lore History panel
    (move is structural, not a content edit) — undo by moving it back.

    Splits into the same auto/confirm modes as the other mutating tools. The
    confirm path stores the move intent (document_id/parent_id/after_id); apply
    re-validates against live tree state.
    """
    user = ctx["user"]
    target, _ = await _gate_move_target(
        document_id=body.document_id, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"), verb="move", past="moved",
    )
    # References are movable; the node-shaped edges live in move_document_command.

    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("move_document")
    return await move_document_command(
        document_id=body.document_id, parent_id=body.parent_id,
        after_id=body.after_id, project_id=ctx["project_id"],
        scope_root=ctx.get("scope_root"), node_type=body.node_type,
    )


@track_agent_tool("rename_document")
async def tool_rename_document(
    body: ToolRenameDocument, ctx: dict = Depends(get_agent_context),
):
    """Rename any node — a tree document or an attached file node, same call
    (move's handler shape over the rename core; the doc-vs-reference split is
    invisible here — the core routes the broadcast off the row). `title` is
    stripped and must be non-empty; a same-title call is an idempotent success
    (`unchanged: true` rides along). NOT reversible via the Lore History panel
    (rename is structural) — undo by renaming back.
    """
    user = ctx["user"]
    await _gate_move_target(
        document_id=body.document_id, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"), verb="rename", past="renamed",
    )

    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )
    if decision.mode != "auto":
        # WHY: no re-gate after the hold, unlike move's apply-time re-check in
        # move_document_command. The approval ask the user actually decides on
        # happens DRIVER-side (dsh's approval service retries this call with the
        # verdict marker) — so the gate above already runs on apply-time state,
        # and this refusal fires only on a confirm cell without approval: no
        # window to re-check across.
        _refuse_unconfirmable("rename_document")
    from documents.service import rename_document

    result = await rename_document(
        document_id=body.document_id, title=body.title,
    )
    # The core says "renamed"; the tool surface says "applied" (parity with
    # move) and `unchanged` rides along on a no-op — idempotent success.
    return {**result, "status": "applied"}
