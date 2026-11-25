"""Pipeline bindings service — ONE body, two callers (editor REST + agent tools).

The bodies of the editor's agent-config CRUD/run routes (routes/extractor.py)
and the pipeline-schedule CRUD routes (routes/pipeline_schedules.py): the REST
routes are one-line delegations and the tool-api handlers call the SAME
functions, so the two surfaces cannot drift on an access check
(require_project_full / require_digest_editor / require_digest_deleter /
resolve_digest_subjects / require_tier + their INVARIANT(security) blocks) — do
not weaken one "for the tool"; the tool adds its own scope narrowing ON TOP
(scope ⊂ RBAC).

# see SYSTEM: extractor-routes (the REST half), SYSTEM: pipeline-schedules
# (the schedule half), SYSTEM: pipeline-registry (the addressable names).
"""
from __future__ import annotations

import json
import logging
from uuid import uuid4

from fastapi import HTTPException
from tiers import require_tier

from access import (
    require_digest_deleter,
    require_digest_editor,
    require_project_full,
    resolve_digest_subjects,
)
from db import (
    build_set_clause,
    create_record,
    extract_id,
    fetch_one,
    get_db,
    serialize_record,
    soft_delete,
)
from models import (
    AgentConfigCreate,
    AgentRunRequest,
    PipelineScheduleCreate,
    PipelineSchedulePatch,
)
from pipeline.core.registry import foreign_scoped_param, pipeline_names
from pipeline.digest.triggers import enqueue_digest
from pipeline.extractor.params import resolve_extractor_params
from pipeline.extractor.runner import enqueue_extract

logger = logging.getLogger(__name__)

# ─── agent_configs bindings ──────────────────────────────────────────────────


async def _guard_row(table: str, entity_id: str, *, not_found: str) -> dict:
    """Fetch one row or raise the caller's uniform 404 (no existence oracle) —
    routes/_guards.guard_row inlined: the import-shape gate forbids a non-routes
    module importing routes.*, and this service is that module."""
    row = await fetch_one(table, entity_id)
    if not row:
        raise HTTPException(status_code=404, detail=not_found)
    return row


def _binding_key_rows_filter() -> str:
    # WHY: `trigger_event IS NONE` matches legacy rows created before the field
    # was added — without this, upsert wouldn't find them and would create duplicates.
    # `kind IS NONE` is the same for rows predating `kind`: they are extractor rows.
    # Kind is part of the key, so a digest and an extractor coexist on one document.
    return (
        "document_id = $did "
        "AND (trigger_event = $evt OR (trigger_event IS NONE AND $evt = 'transcription_complete')) "
        "AND (kind = $kind OR (kind IS NONE AND $kind = 'extractor'))"
    )


async def _write_binding(
    db, body: AgentConfigCreate, project_id: str, live: list, revived: list,
    created_by: str, subjects,
) -> dict:
    """The UPDATE-or-CREATE half of the upsert (same upsert key as REST)."""
    fields = {
        "config_doc_id": body.config_doc_id, "target_doc_id": body.target_doc_id,
        "project_id": project_id, "title_template": body.title_template, "model": body.model,
        "trigger_event": body.trigger_event, "kind": body.kind,
        "created_by": created_by, "subject_user_ids": subjects,
    }
    existing = live or revived
    if existing:
        config_id = extract_id(existing[0].get("id"))
        assignments = build_set_clause(fields)
        await db.query(
            f"UPDATE type::record('agent_configs', $id) SET {assignments}, deleted_at = NONE",
            {"id": config_id, **fields},
        )
        updated = await fetch_one("agent_configs", config_id)
        return serialize_record(updated, "config_id")

    record = await create_record("agent_configs", str(uuid4()), {"document_id": body.document_id, **fields})
    return serialize_record(record, "config_id")


async def upsert_agent_config(body: AgentConfigCreate, user: dict) -> dict:
    """Create-or-update one binding (the editor's PUT /agent-config body)."""
    db = await get_db()
    config_doc = await fetch_one("documents", body.config_doc_id)
    if not config_doc:
        raise HTTPException(status_code=400, detail="Config document not found")
    target_doc = await fetch_one("documents", body.target_doc_id)
    if not target_doc:
        raise HTTPException(status_code=400, detail="Target document not found")
    source_doc = await fetch_one("documents", body.document_id)
    if not source_doc:
        raise HTTPException(status_code=400, detail="Source document not found")

    project_id = source_doc.get("project_id", "")
    if config_doc.get("project_id") != project_id or target_doc.get("project_id") != project_id:
        raise HTTPException(status_code=400, detail="Config and target documents must be in the same project as source")

    await require_project_full(project_id, user)
    if body.kind != "digest" and body.subject_user_ids is not None:
        raise HTTPException(status_code=400, detail="subject_user_ids applies only to a digest binding")

    key_vars = {"did": body.document_id, "evt": body.trigger_event, "kind": body.kind}
    live = await db.query(
        f"SELECT * FROM agent_configs WHERE {_binding_key_rows_filter()} AND deleted_at IS NONE LIMIT 1", key_vars,
    )
    revived = [] if live else await db.query(
        f"SELECT * FROM agent_configs WHERE {_binding_key_rows_filter()} AND deleted_at IS NOT NONE LIMIT 1", key_vars,
    )

    # created_by is stamped once and never changed by an update; a revived
    # soft-deleted row is a new binding, so its caller becomes the creator.
    previous = live[0] if live else {}
    created_by = previous.get("created_by") or user["user_id"]
    subjects = None
    if body.kind == "digest":
        if live:
            await require_digest_editor(project_id, user, previous.get("created_by"))
        requested = body.subject_user_ids if body.subject_user_ids is not None else previous.get("subject_user_ids")
        subjects = await resolve_digest_subjects(project_id, user, created_by, requested)

    return await _write_binding(db, body, project_id, live, revived, created_by, subjects)


async def delete_agent_config(config_id: str, user: dict) -> dict:
    """Soft-delete one binding; a DIGEST binding's schedule rows go with it."""
    db = await get_db()
    config = await _guard_row("agent_configs", config_id, not_found="Agent config not found")
    await require_project_full(config.get("project_id", ""), user)
    if config.get("kind") == "digest":
        await require_digest_deleter(config.get("project_id", ""), user, config.get("created_by"))
    await soft_delete("agent_configs", config_id)

    # WHY: a digest binding owns its schedule rows — deleting it while they keep
    # ticking leaves a schedule dispatching into a missing binding (the tick
    # records last_status='error' every interval forever). Match is EXACT on
    # json.loads(params_json)["config_id"] (a handful of rows per project; no
    # string-contains over JSON, no new field). Extractor schedules are NOT
    # cascaded: their frozen params name config/target DOCUMENTS, not a binding
    # (see pipeline/core/registry.py), so no exact link exists — a REST-era
    # extractor schedule keeps ticking exactly as it did before this change.
    if config.get("kind") == "digest":
        rows = await db.query(
            "SELECT id, params_json FROM pipeline_schedules "
            "WHERE project_id = $pid AND pipeline = 'digest' AND deleted_at IS NONE",
            {"pid": config.get("project_id", "")},
        )
        for row in rows or []:
            try:
                params = json.loads(row.get("params_json") or "{}")
            except (TypeError, ValueError):
                continue
            if params.get("config_id") == config_id:
                await soft_delete("pipeline_schedules", extract_id(row.get("id")))
    return {"ok": True}


async def run_agent(body: AgentRunRequest, user: dict) -> dict:
    """The manual trigger (editor POST /agent-config/run): one digest binding,
    or every extractor row bound to the reference's parent document."""
    if body.config_id:
        # The digest branch's 'ai' gate lives in run_digest_binding (first line)
        # — the agent's run_pipeline tool reaches that function directly, so the
        # gate must sit where BOTH callers pass through, not in this wrapper.
        return await _run_digest_binding(body.config_id, user)
    # see SYSTEM: capability-tiers — 'ai' gate: a pipeline launch is a model run;
    # refused before any project/config resolution so the tier 403 is the
    # first answer.
    await require_tier(user, "ai")
    # ARCH: param resolution is shared with the MCP `run_extractor` tool via
    # resolve_extractor_params — the editor and the benchmark resolve the SAME way
    # (reference → parent source → agent_configs), so the two paths cannot drift.
    params_list = await resolve_extractor_params(body.document_id, user)

    # INVARIANT(security): the pipeline writes its own output — this call only enqueues,
    # and the worker creates the document under the launch consent granted above.
    # Why: independence from the caller is what a pipeline IS; a caller does not need,
    # and is not given, a separate right to save the result. Both halves of the model
    # live in the module docstring of pipeline.extractor.params.
    for params in params_list:
        await _post_extractor(params, user.get("user_id"))

    return {"status": "accepted"}


async def _run_digest_binding(config_id: str, user: dict) -> dict:
    """The editor's response shape: always `accepted` (a collapsed post is
    still a pending run — nothing new to report on this surface)."""
    await run_digest_binding(config_id, user)
    return {"status": "accepted"}


async def _pending_run_id(config_id: str) -> str | None:
    """The newest queued/running pipeline_runs row of a binding, or None."""
    db = await get_db()
    rows = await db.query(
        "SELECT id FROM pipeline_runs WHERE config_id = $c "
        "AND status IN ['queued', 'running'] ORDER BY queued_at DESC LIMIT 1",
        {"c": config_id},
    )
    return extract_id(rows[0].get("id")) if rows else None


async def run_digest_binding(config_id: str, user: dict) -> dict:
    """Run ONE digest binding now; returns the launch outcome for callers that
    read the run log (the agent's run_pipeline tool):
    {status: accepted, run_id} — or {status: already_pending, run_id: <the
    newest queued/running row of this binding, or null>} when arq collapsed
    the post onto a pending `digest:{config_id}` job — never a second run."""
    # see SYSTEM: capability-tiers — 'ai' gate: a pipeline launch is a model run;
    # refused before any project/config resolution so the tier 403 is the
    # first answer. It lives HERE, not only in run_agent: the agent's
    # run_pipeline tool (routes/tool_api/pipelines.py) calls this function
    # directly, and that surface is reachable by a key with no ai-gated chat
    # turn in front of it — the REST wrapper's gate never runs on that path.
    await require_tier(user, "ai")
    config = await _guard_row(
        "agent_configs", config_id, not_found="Agent config not found",
    )
    project_id = extract_id(config.get("project_id", ""))
    # Launch consent, as for the extractor above: the digest then writes its own report.
    await require_project_full(project_id, user)
    if config.get("kind") != "digest":
        raise HTTPException(status_code=400, detail="An extractor runs per reference: pass its document_id")
    await require_digest_editor(project_id, user, config.get("created_by"))
    run_id = await enqueue_digest(config_id, project_id, trigger="manual")
    if run_id is not None:
        return {"status": "accepted", "run_id": run_id}
    return {"status": "already_pending", "run_id": await _pending_run_id(config_id)}


async def _post_extractor(params, user_id: str | None) -> str | None:
    """One manual extractor run on resolved params (run id, or None on a collapse)."""
    return await enqueue_extract(
        reference_id=params.reference_id, source_doc_id=params.source_doc_id,
        config_doc_id=params.config_doc_id, target_doc_id=params.target_doc_id,
        project_id=params.project_id,
        title_template=params.title_template, model=params.model,
        user_id=user_id, trigger="manual", config_id=params.config_id,
    )


async def run_extractor_binding(config_id: str, reference_id: str, user: dict) -> dict:
    """Run ONE extractor binding on ONE reference — the agent's per-binding run
    (NOT run_agent's body, which takes a document and runs every row).
    `reference_id` is resolved via resolve_extractor_params and filtered to
    THIS binding's config/target docs; a reference of another source document
    is a 400, not a silent wrong run."""
    # see SYSTEM: capability-tiers — same 'ai' gate as the editor's run_agent:
    # a pipeline launch is a model run.
    await require_tier(user, "ai")
    config = await _guard_row(
        "agent_configs", config_id, not_found="Agent config not found",
    )
    if config.get("kind") not in (None, "extractor"):
        raise HTTPException(status_code=400, detail="A digest runs per binding without a reference_id")
    project_id = extract_id(config.get("project_id", ""))
    # INVARIANT(security): same launch-consent model as run_agent — permission
    # is checked once, here; the pipeline then writes its own output.
    # Why: independence from the caller is what a pipeline IS (params.py's
    # model); a per-binding run re-derives nothing the launch already granted.
    await require_project_full(project_id, user)

    params_list = await resolve_extractor_params(reference_id, user)
    # By the binding row id ONLY: resolve_extractor_params stamps every row's
    # config_id, and the upsert key (source + trigger + kind) lets two extractor
    # rows on one source share config + target — a config/target match would
    # then attribute this binding's run to (and launch) the OTHER row.
    matching = [p for p in params_list if p.config_id == config_id]
    if not matching:
        raise HTTPException(
            status_code=400,
            detail="Reference is not attached to this binding's source document",
        )
    run_id = await _post_extractor(matching[0], user.get("user_id"))
    if run_id is not None:
        return {"status": "accepted", "run_id": run_id}
    return {"status": "already_pending", "run_id": await _pending_run_id(config_id)}


# ─── pipeline schedules ──────────────────────────────────────────────────────


async def _load_owned_schedule(schedule_id: str, user: dict) -> dict:
    """Load a schedule and enforce full access on ITS project — uniform 404 on
    missing/soft-deleted/cross-project ids (no existence oracle), the
    load_owned_proposal discipline."""
    row = await _guard_row(
        "pipeline_schedules", schedule_id, not_found="Schedule not found",
    )
    await require_project_full(row.get("project_id", ""), user)
    return row


async def _digest_schedule_author(row: dict) -> str | None:
    """`created_by` of the live binding a digest schedule runs; None when its
    config_id is unreadable or resolves to no live binding of the schedule's project."""
    try:
        params = json.loads(row.get("params_json") or "{}")
    except (TypeError, ValueError):
        params = None
    config_id = params.get("config_id") if isinstance(params, dict) else None
    binding = None
    if config_id and isinstance(config_id, str):
        binding = await fetch_one("agent_configs", config_id)
    if not binding or extract_id(binding.get("project_id")) != row.get("project_id"):
        logger.warning(
            "Digest schedule %s has no live binding in its project (config_id=%r): judged as authorless",
            extract_id(row.get("id")), config_id,
        )
        return None
    return binding.get("created_by")


async def _require_digest_schedule(row: dict, user: dict, *, stops: bool, steers: bool) -> None:
    """The digest-author gate on an existing schedule; other pipelines pass.
    `stops` = disable or delete, `steers` = enable or change the interval."""
    if row.get("pipeline") != "digest":
        return
    project_id = row.get("project_id", "")
    # INVARIANT(security): stopping a digest's schedule needs the right to delete its binding, starting or re-timing it the right to run it; project-full alone is neither.
    # Why: the digest reads its author's chats, and the schedule is a second handle on the same binding.
    # WHY authorless, not the schedule's own created_by: an unresolvable binding must not hand out rights; a project root (and, to stop or delete, an instance admin) can still remove the row.
    author = await _digest_schedule_author(row)
    if stops:
        await require_digest_deleter(project_id, user, author)
    if steers:
        await require_digest_editor(project_id, user, author)


def serialize_schedule(row: dict) -> dict:
    out = serialize_record(row, "schedule_id")
    # params_json → params (parsed; a corrupt blob surfaces as {} — the row was
    # validated at create, and the tick re-guards with its own error recording).
    try:
        out["params"] = json.loads(out.pop("params_json", "") or "{}")
    except (TypeError, ValueError):
        out["params"] = {}
    return out


async def _validate_schedule_params(spec, params: dict, project_id: str) -> None:
    """Reject params that cannot dispatch safely — at CREATE, where the caller
    still gets an actionable 400 instead of a schedule that errors every tick.

    # INVARIANT(security): the frozen params are the kwargs the worker runs
    # with NO user context — this gate substitutes per-call access checks.
    # Why: a full-access member of project A must not schedule a run whose ids
    # point into project B (the extractor reads the source and writes the child
    # document wherever the params say). Binds the signature (no foreign/reserved
    # keys), binds params["project_id"] to the schedule's project, and resolves
    # every declared id-param inside that project.
    """
    import inspect

    # Reserved key: `run_id` is a REAL task kwarg (the pipeline_runs log id), so
    # the signature bind below would ACCEPT it — but it is minted by the dispatch
    # side, never user-chosen. A caller-frozen run_id would let a schedule write
    # its outcome onto an arbitrary run row.
    if "run_id" in params:
        raise HTTPException(
            status_code=400,
            detail="params['run_id'] is reserved — the dispatch mints the run id",
        )
    try:
        # bind with a placeholder first arg: arq injects the worker ctx
        # positionally — it is never a schedule param.
        inspect.signature(spec.entry).bind(None, **params)
    except TypeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"params do not match the {spec.name} task signature: {e}",
        ) from e
    # enqueue()'s own keyword channel (job_id/queue/defer) is rejected by the
    # signature bind above; params must stay a plain dict of task kwargs.
    scoped = spec.project_scoped_params
    if "project_id" in scoped and params.get("project_id") != project_id:
        raise HTTPException(
            status_code=400,
            detail=f"params['project_id'] must be this schedule's project ({project_id})",
        )
    foreign = await foreign_scoped_param(spec, params, project_id)
    if foreign:
        raise HTTPException(
            status_code=400,
            detail=f"params['{foreign}'] does not resolve to a record in this project",
        )


async def create_schedule(body: PipelineScheduleCreate, user: dict) -> dict:
    # see SYSTEM: capability-tiers — 'ai' gate: a schedule is an ONGOING model
    # cost; creating one is an ai-tier act (the tick also re-checks the
    # creator at fire time, so a downgrade stops it).
    await require_tier(user, "ai")
    await require_project_full(body.project_id, user)
    # Registry-derived validation (never a hand-list) — the error names the known
    # pipelines so the fix is actionable.
    if body.pipeline not in pipeline_names():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown pipeline {body.pipeline!r}; known: {pipeline_names()}",
        )
    from pipeline.core.registry import get_pipeline

    spec = get_pipeline(body.pipeline)
    assert spec is not None  # pipeline_names() validated membership above
    await _validate_schedule_params(spec, body.params, body.project_id)
    if spec.name == "digest":
        binding = await fetch_one("agent_configs", str(body.params["config_id"]))
        await require_digest_editor(body.project_id, user, binding.get("created_by"))
    schedule_id = str(uuid4())
    record = await create_record("pipeline_schedules", schedule_id, {
        "project_id": body.project_id,
        "pipeline": body.pipeline,
        "interval_s": body.interval_s,
        "enabled": body.enabled,
        "params_json": json.dumps(body.params),
        "created_by": user.get("user_id", ""),
    })
    return serialize_schedule(record)


async def patch_schedule(schedule_id: str, body: PipelineSchedulePatch, user: dict) -> dict:
    db = await get_db()
    current = await _load_owned_schedule(schedule_id, user)
    # see SYSTEM: capability-tiers — re-ENABLING a schedule is a new cost start
    # (the 'ai' gate); disabling or changing the interval is not.
    if body.enabled is True:
        await require_tier(user, "ai")
    await _require_digest_schedule(
        current, user, stops=body.enabled is False,
        # WHY: an empty patch counts as steering — it still writes updated_at.
        steers=body.enabled is not False or body.interval_s is not None,
    )
    sets = ["updated_at = time::now()"]
    if body.enabled is not None:
        sets.append("enabled = $enabled")
    if body.interval_s is not None:
        sets.append("interval_s = $interval_s")
    await db.query(
        "UPDATE type::record('pipeline_schedules', $id) SET " + ", ".join(sets),
        {
            "id": schedule_id,
            **({"enabled": body.enabled} if body.enabled is not None else {}),
            **({"interval_s": body.interval_s} if body.interval_s is not None else {}),
        },
    )
    row = await fetch_one("pipeline_schedules", schedule_id)
    return serialize_schedule(row)


async def delete_schedule(schedule_id: str, user: dict) -> dict:
    row = await _load_owned_schedule(schedule_id, user)
    await _require_digest_schedule(row, user, stops=True, steers=False)
    await soft_delete("pipeline_schedules", schedule_id)
    return {"success": True}


__all__ = [
    "create_schedule",
    "delete_agent_config",
    "delete_schedule",
    "patch_schedule",
    "run_agent",
    "run_digest_binding",
    "run_extractor_binding",
    "upsert_agent_config",
]
