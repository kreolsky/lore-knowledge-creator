"""Tool-API — saving facts from THIS chat to project memory, and their placements (save_chat_facts / set_fact_anchor)."""
from agent.context import get_agent_context, owned_session_row
from doc_edit_lock import edit_lock
from fastapi import Body, Depends, HTTPException
from surrealdb import AsyncSurreal

from access import get_document_access, require_full_project_access
from db import get_db
from models import ToolSaveChatFacts, ToolSetFactAnchor
from routes.tool_api._common import (
    _refuse_memory_disabled,
    _refuse_unconfirmable,
    _require_in_app_agent,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool

# ─── save: facts from THIS chat, and their placements ─────────────────────────


async def _admit_pack_write(ctx: dict) -> AsyncSurreal:
    """The WRITE admission every save-side tool opens with: the external-key
    wall, then the `memory` off-switch (the same gate as every consolidation
    write). The `chat-recall` switch does not gate saving — it governs reading
    the user's other chats, not memory writes."""
    _require_in_app_agent(ctx)
    db = await get_db()
    await _refuse_memory_disabled(db)
    return db


@track_agent_tool("save_chat_facts")
async def tool_save_chat_facts(
    body: ToolSaveChatFacts = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Save facts distilled from THIS conversation to project memory.

    Moderated capture: the agent proposes one
    verdict per fact, the user moderates on the approval card. In `confirm`
    (the default) one batch produces ONE card — a rejection with text is
    per-fact moderation and the agent re-emits the corrected batch; in `auto`
    there is no card and the facts are written directly.

    The order of gates follows the consolidation pack's reopen path
    (routes/tool_api/memory.py `_propose_or_apply_reopen`): full-access check,
    the caller's OWN live session (no session ⇒ 400), the confirm-gate refusal
    — and ONLY THEN `mint_memory_run_key`, so an asked or rejected call mints
    no key. The run key is the same Memory-scoped wall as a consolidation run;
    `record_active_run` is NOT called — the save is one shot and must not
    hijack a session's active consolidation run. The chat source is STAMPED
    from the session id server-side (memory.apply.apply_chat_verdicts); the
    model never supplies it.
    """
    await _admit_pack_write(ctx)
    user = ctx["user"]
    await _admit_save_caller(ctx)
    if (await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )).mode != "auto":
        _refuse_unconfirmable("save_chat_facts")

    from memory.apply import apply_chat_verdicts
    from memory.run_key import mint_memory_run_key

    run = await mint_memory_run_key(
        user_id=ctx["user_id"], project_id=ctx["project_id"],
    )
    result = await apply_chat_verdicts(
        project_id=ctx["project_id"], run_id=run.run_id,
        verdicts=[v.model_dump() for v in body.verdicts],
        session_id=ctx["session_id"], user=user,
    )
    return {"status": "applied", **result}


async def _admit_save_caller(ctx: dict) -> None:
    """The save-side caller gates, in the plan's fixed order: FULL project
    access first (saving publishes project-wide memory), then the caller's
    OWN live session (no own session ⇒ 400 — nothing to save from)."""
    await require_full_project_access(ctx["project_id"], ctx["user"],
                                      "save chat facts")
    session = await owned_session_row(ctx) if ctx.get("session_id") else None
    if not session:
        raise HTTPException(
            status_code=400,
            detail=(
                "save_chat_facts saves from the calling chat session — this "
                "key has no own live session for this project"
            ),
        )


@track_agent_tool("set_fact_anchor")
async def tool_set_fact_anchor(
    body: ToolSetFactAnchor = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Place one stored memory fact on specific documents (`mem.anchors`), or
    clear the placement with `document_ids: []`.

    Anchors are PLACEMENT, not origin: non-empty anchors REPLACE the
    provenance join for that fact in a subtree narrow
    (retrieval._allowed_fact_ids), clearing them returns the fact to following
    its chat/reference source. The fact keeps its row, its `parent_id` and its
    provenance. Targets are validated BEFORE the confirm gate (parity with
    reopen's validate-before-propose: a confirm-mode call must not propose a
    placement that could never apply) and the write runs under the project's
    memory lock — the same lock an apply holds, so an anchor write and a merge
    on the same fact cannot interleave.
    """
    db = await _admit_pack_write(ctx)
    user = ctx["user"]
    project_id = ctx["project_id"]
    await require_full_project_access(project_id, user, "set fact anchors")
    await _require_anchorable_fact(db, project_id, body.fact_id)
    anchors = await _validated_anchor_targets(
        db, project_id, user, body.document_ids,
    )
    if (await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )).mode != "auto":
        _refuse_unconfirmable("set_fact_anchor")
    await _write_fact_anchors(db, project_id, body.fact_id, anchors)
    return {"status": "applied", "fact_id": body.fact_id, "anchors": anchors}


async def _write_fact_anchors(
    db, project_id: str, fact_id: str, anchors: list[str],
) -> None:
    """Write `mem.anchors` under the project's memory lock — the same lock an
    apply holds, so an anchor write and a merge on the same fact cannot
    interleave."""
    async with edit_lock(f"memory:{project_id}"):
        # Re-read `mem` UNDER the lock, on the LIVE predicate only: an apply
        # holding the same lock may have rewritten `mem` between the validation
        # read and here (a merge unions anchors into the survivor — a stale
        # copy here would drop that union), and a merge may have RETIRED the
        # fact outright — a retired row answers 404, never takes the write.
        rows = await db.query(
            "SELECT mem FROM documents "
            "WHERE meta::id(id) = $fid AND project_id = $pid "
            "AND is_memory = true AND deleted_at IS NONE",
            {"fid": fact_id, "pid": project_id},
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Fact document not found")
        mem = dict(rows[0].get("mem") or {})
        if anchors:
            mem["anchors"] = anchors
        else:
            mem.pop("anchors", None)
        await db.query(
            "UPDATE type::record('documents', $id) SET mem = $mem",
            {"id": fact_id, "mem": mem},
        )


async def _require_anchorable_fact(db, project_id: str, fact_id: str) -> None:
    """Uniform 404 for a fact id that is not a LIVE memory fact of this
    project — missing, soft-deleted (retired/merged), cross-project and
    non-memory all read the same (no existence oracle)."""
    rows = await db.query(
        "SELECT VALUE meta::id(id) FROM documents "
        "WHERE meta::id(id) = $fid AND project_id = $pid "
        "AND is_memory = true AND deleted_at IS NONE",
        {"fid": fact_id, "pid": project_id},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Fact document not found")


async def _validated_anchor_targets(
    db, project_id: str, user: dict, document_ids: list[str],
) -> list[str]:
    """The normalized anchor list (order-preserving dedup) after validating
    every target: a live, non-reference, non-memory document of the project
    with at least commentator access — the same rule a chat reparent applies
    (`_reparent_sets`, chat_sessions/update.py). A dead/foreign id is a
    uniform 404."""
    anchors: list[str] = []
    for did in document_ids:
        d = str(did)
        if d and d not in anchors:
            anchors.append(d)
    if not anchors:
        return []
    rows = await db.query(
        "SELECT meta::id(id) AS id, is_reference, is_memory FROM documents "
        "WHERE meta::id(id) IN $ids AND project_id = $pid "
        "AND deleted_at IS NONE",
        {"ids": anchors, "pid": project_id},
    ) or []
    by_id = {r["id"]: r for r in rows}
    for did in anchors:
        row = by_id.get(did)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"anchor target {did} is not a live document of this project",
            )
        if row.get("is_reference"):
            raise HTTPException(
                status_code=400, detail="Cannot anchor a fact to a reference",
            )
        if row.get("is_memory"):
            raise HTTPException(
                status_code=400, detail="Cannot anchor a fact to another fact",
            )
    # Independent per-target access resolution — never `or`-combined (the
    # nullable-fields rule): one readable target cannot carry an unreadable one.
    for did in anchors:
        access = await get_document_access(did, user)
        if access is None or access == "readonly":
            raise HTTPException(
                status_code=403,
                detail="Commentator access required on the target document",
            )
    return anchors
