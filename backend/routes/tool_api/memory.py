"""Tool-API — project memory (`consolidate_memory` / `next_reference` / verdicts).

# ARCH (import DAG): imports _router + _common only; reaches the `memory` package
# lazily inside the handler, matching every other domain module here.
"""
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from access import get_project_access
from models import (
    ToolApplyMemoryVerdicts,
    ToolConsolidateMemory,
    ToolGetFactHistory,
    ToolGetMemoryFacts,
    ToolNextReference,
    ToolReopenConsolidation,
)
from routes.tool_api._common import (
    _refuse_unconfirmable,
)
from routes.tool_api_telemetry import track_agent_tool


@track_agent_tool("consolidate_memory")
async def tool_consolidate_memory(
    body: ToolConsolidateMemory = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Start (or continue) a consolidation run: mint the run, serve its FIRST portion.

    # ARCH (design P13): the TOOL is the capability and the boundary; the framing
    # is delivered by the memory-consolidation skill (on demand). It is therefore in
    # AGENT_TOOLS and callable under ANY persona including Default — if using memory
    # required selecting a persona it would be a mode, not a memory, and the future
    # ambient path (which has no persona at all) would start life needing a rewrite.

    # ARCH: the payload carries ONE
    # reference (the oldest unconsumed under the host). The rest of the run is walked
    # with `next_reference`; "what is left" is the absence of a stamp
    # (`documents.mem_consolidated_at`), so this call RESUME-able by construction —
    # a run that died mid-way simply continues here.

    # INVARIANT(security): the run key's PLAINTEXT is never placed in this response.
    # Why: whatever the agent is shown it can quote — into a document body or a chat
    # message that is then persisted, rendered and embedded. Only `run_id` (the key
    # id) is returned; it is all the agent needs, since facts cite the run as
    # provenance. Note the plaintext is not "kept for later" either — it dies with
    # the minting call and only its hash is stored (see memory/run_key.py).

    Access note: the target is gated by the CALLING key's project, so a target in
    another project 404s uniformly (no existence oracle). The run key this mints is
    narrower than the caller's, never wider — it is sandboxed to `Memory` while the
    caller's chat key is whole-project.
    """
    from memory.run_key import mint_memory_run_key, record_active_run
    from memory.task_builder import build_consolidation_task, resolve_consolidation_host

    project_id = ctx["project_id"]
    # Validate the target (uniform 404, no existence oracle) BEFORE minting — an
    # invalid target must not persist a run key row (credential minting is the one
    # thing this call does that a repeated bogus call should not accumulate).
    await resolve_consolidation_host(
        project_id=project_id, target_doc_id=body.target_doc_id,
    )
    run = await mint_memory_run_key(
        user_id=ctx["user_id"], project_id=project_id,
    )
    # Record this session's active run so the loop tools resolve it without a run_id.
    await record_active_run(ctx.get("session_id"), run.run_id)
    task = await build_consolidation_task(
        project_id=project_id, target_doc_id=body.target_doc_id, run_id=run.run_id,
    )
    return {"run_id": run.run_id, **task}


@track_agent_tool("reopen_consolidation")
async def tool_reopen_consolidation(
    body: ToolReopenConsolidation = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Clear a target's consolidation stamps so its material can be walked again.

    The unit is the TARGET: a reference id re-resolves to its host and the clear covers
    that host's whole stack (same `resolve_consolidation_host` as `consolidate_memory`).
    Mutating (it moves the run cursor), so `mutating: true` puts it behind the apply gate
    — confirm ⇒ a proposal the user approves; auto ⇒ the clear runs directly. The clear
    writes NONE to both stamp fields and stops — no fact is touched, nothing is recorded
    about why.

    Agent-only (never on MCP): same posture as the rest of the consolidation pack — the MCP
    surface is force-auto with no confirmation tier, and this call destroys the cursor.
    Access (full project access) is checked here (the tool's edge), deliberately NOT
    unified with any other edge. The target is gated by the calling key's project, so a
    target in another project 404s uniformly (no existence oracle).
    """
    from memory.task_builder import resolve_consolidation_host

    user = ctx["user"]
    access = await get_project_access(ctx["project_id"], user)
    if access != "full":
        raise HTTPException(
            status_code=403,
            detail="Full project access required to reopen a consolidation",
        )
    # Validate the target (uniform 404, no existence oracle) BEFORE proposing — parity
    # with reprocess's validate-before-propose: a confirm-mode call must not propose a
    # reopen that could never apply.
    await resolve_consolidation_host(
        project_id=ctx["project_id"], target_doc_id=body.target_doc_id,
    )
    return await _propose_or_apply_reopen(ctx, body, access, user)


async def _propose_or_apply_reopen(
    ctx: dict, body: ToolReopenConsolidation, access: str, user: dict,
) -> dict:
    """Resolve apply-mode for a validated reopen: auto clears directly; confirm holds
    with the ask signal (409 confirmation_required) — moving the run cursor stays
    gated until the user approves."""
    from agent.apply_policy import resolve_apply_mode

    if resolve_apply_mode(
        ui_preference=body.apply.value, is_system=False,
    ).mode != "auto":
        _refuse_unconfirmable("reopen_consolidation")

    from memory.task_builder import reopen_consolidation

    result = await reopen_consolidation(
        project_id=ctx["project_id"], target_doc_id=body.target_doc_id,
    )
    return {"status": "applied", **result}


@track_agent_tool("next_reference")
async def tool_next_reference(
    body: ToolNextReference = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Continue the loop: stamp the completed reference, serve the next portion.

    Returns the next portion in the SAME shape as `consolidate_memory`, or
    `complete: true` with the run's totals when nothing is left. Stamping happens
    here — when the agent SAYS it is done with the reference, after its verdicts were
    applied — so a stamp never precedes the apply that justifies it. A reference that
    was already stamped is not re-stamped (first writer wins) but the next portion is
    served anyway, so a retried call after a lost response cannot strand the run.

    `run_id` resolves from the session's active run when omitted; a wrong run_id still 404s.
    """
    from memory.run_key import resolve_run_id
    from memory.task_builder import next_reference

    run_id = await resolve_run_id(
        run_id=body.run_id, session_id=ctx.get("session_id"),
    )
    return await next_reference(
        project_id=ctx["project_id"], run_id=run_id,
        completed_reference_id=body.completed_reference_id,
        completed_window_index=body.completed_window_index,
    )


@track_agent_tool("get_memory_facts")
async def tool_get_memory_facts(
    body: ToolGetMemoryFacts = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Read the BODIES of specific facts — the on-demand half of the two-channel
    delivery. The index names every fact; this fetches what the adjudication step is
    about to look at. Returns the facts found, plus the ids that resolved to nothing
    (so a stale index entry is reported, never silently ignored)."""
    from memory.task_builder import get_memory_facts

    return await get_memory_facts(
        project_id=ctx["project_id"], ids=body.ids,
    )


@track_agent_tool("get_fact_history")
async def tool_get_fact_history(
    body: ToolGetFactHistory = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Read the version history of one fact — the explicit-request reader for the
    fields `get_memory_facts` carries only as a marker. A served live fact with
    `revisions > 0` was superseded; this returns the retired wording + each
    predecessor's supersede reason + provenance, newest first. `fact_id` is the LIVE
    fact the agent sees (a retired id is a 404)."""
    from memory.task_builder import get_fact_history

    return await get_fact_history(
        project_id=ctx["project_id"], fact_id=body.fact_id,
    )


@track_agent_tool("apply_memory_verdicts")
async def tool_apply_memory_verdicts(
    body: ToolApplyMemoryVerdicts = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Apply a batch of per-fact verdicts to project memory.

    # ARCH: a DIRECT write path, deliberately outside the proposal/confirmation flow.
    # Why (this is the plan's settled routing decision, not an oversight): the chat
    # proposal-apply path does NOT thread `scope_root`, so a run's writes routed
    # through it would silently stop being scoped — no error, no log, the write just
    # lands anywhere. The batch is also a transaction: applying half a set of
    # verdicts leaves the batch half-written: some facts revised, their twins not, and
    # nothing naming which. Per-write approval cards are the wrong shape for it too — in a
    # deliberative session each apply confirms what the human just argued through.

    The run's own scope wall is resolved from its key row; the caller's key gates the
    project. Returns what was written and — load-bearing — what was SKIPPED.

    `run_id` resolves from the session's active run when omitted; a wrong run_id still 404s.
    """
    from memory.apply import apply_memory_verdicts
    from memory.run_key import resolve_run_id

    run_id = await resolve_run_id(
        run_id=body.run_id, session_id=ctx.get("session_id"),
    )
    return await apply_memory_verdicts(
        project_id=ctx["project_id"], run_id=run_id,
        verdicts=[v.model_dump() for v in body.verdicts],
        reference_id=body.reference_id, user=ctx["user"],
    )
