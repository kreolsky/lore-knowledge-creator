"""Tool-API structural mutating tools — move_document, rename_document, restore_document_version.

# ARCH: move is a STRUCTURAL executor, not a content
# mutation. It reparents a tree document and/or reorders it within a sibling group
# through the ONE in-project move (documents.move.move_document_command, shared with
# the PATCH parent_id branch). It NEVER touches the CRDT text, so it is
# NOT reversible via the Lore History panel (stated verbatim in the tool description)
# — reversibility is via re-moving the document.
#
# rename is the SURFACE over the rename core (documents.service.rename_document):
# it adds no rename logic, only the agent-tool shape —
# the shared structural gate, apply resolution, and the mid-turn hold. Like move it
# is structural (a title is metadata, not buffer text), so it is likewise NOT
# reversible via the History panel — undo by renaming back.
#
# parent_id semantics: a value = new parent; null = project root —
# for an UNSCOPED key. Under a subtree-scoped key null/empty resolves to the
# SCOPE root (scope.resolve_scoped_parent, the same normalization create uses) —
# a null move may not escape the key's sandbox.
# after_id: a sibling under the resulting parent to land after; null = top of group.
#
# Import DAG: imports documents.move + _common + scope's memory-fact refusal;
# reaches documents.service / the rest of scope lazily inside the handlers.
"""
from agent.context import get_agent_context
from documents.move import move_document_command
from documents.restore import restore_checkpoint_command
from fastapi import Depends, HTTPException

from access import get_document_access, require_document_full
from db import fetch_one, get_db
from models import ToolMoveDocument, ToolRenameDocument, ToolRestoreDocumentVersion
from routes.tool_api._common import (
    _is_system_target,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api.reads import _raise_soft_error
from routes.tool_api_telemetry import track_agent_tool
from scope import reject_memory_target


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
    ("…required to move") and a memory fact IS movable inside Memory (the
    command holds the Memory boundary both ways) — folding the two gates would
    change refusal text and add the memory rejection.
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

    # A move OUT of or INTO the system root is a self-edit: the doc and the
    # destination parent both decide the is_system cell.
    from scope import resolve_scoped_parent

    pid = ctx["project_id"]
    is_system = await _is_system_target(body.document_id, pid, target) or (
        await _is_system_target(
            resolve_scoped_parent(ctx.get("scope_root"), body.parent_id), pid,
        )
    )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=is_system,
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("move_document", decision.reason)
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
    target, _ = await _gate_move_target(
        document_id=body.document_id, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"), verb="rename", past="renamed",
    )
    # A fact's title is half the fact — it changes only through its one door.
    reject_memory_target(target)

    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=await _is_system_target(body.document_id, ctx["project_id"], target),
    )
    if decision.mode != "auto":
        # WHY: no re-gate after the hold, unlike move's apply-time re-check in
        # move_document_command. The approval ask the user actually decides on
        # happens DRIVER-side (dsh's approval service retries this call with the
        # verdict marker) — so the gate above already runs on apply-time state,
        # and this refusal fires only on a confirm cell without approval: no
        # window to re-check across.
        _refuse_unconfirmable("rename_document", decision.reason)
    from documents.service import rename_document

    result = await rename_document(
        document_id=body.document_id, title=body.title,
    )
    # The core says "renamed"; the tool surface says "applied" (parity with
    # move) and `unchanged` rides along on a no-op — idempotent success.
    return {**result, "status": "applied"}


@track_agent_tool("restore_document_version")
async def tool_restore_document_version(
    body: ToolRestoreDocumentVersion, ctx: dict = Depends(get_agent_context),
):
    """Restore a document to one of its checkpoints through the History panel's
    command (before-restore backup first); returns the backup id as the undo."""
    from agent.history_executors import gated_target, owned_checkpoint

    user = ctx["user"]
    target = await gated_target(
        doc_id=body.document_id, project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )
    if isinstance(target, dict):
        _raise_soft_error(target)
    checkpoint = await owned_checkpoint(body.checkpoint_id, target.resolved)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    await require_document_full(target.resolved, user)
    row = await fetch_one("documents", target.resolved)
    # A restored fact body would bypass its one door (and its provenance).
    reject_memory_target(row or {})
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=await _is_system_target(target.resolved, ctx["project_id"], row),
    )
    if decision.mode != "auto":
        # WHY: same driver-side approval as rename_document — see the note there.
        _refuse_unconfirmable("restore_document_version", decision.reason)
    result = await restore_checkpoint_command(await get_db(), body.checkpoint_id, checkpoint)
    return {"status": "applied", "backup_checkpoint_id": result["backup_checkpoint_id"]}
