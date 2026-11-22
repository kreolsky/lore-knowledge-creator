"""Tool-API — pipelines: the `pipelines` skill's pack (the agent's hands on
extractor/digest bindings, schedules and runs).

Thin by design: the consent gate (the mid-turn approval card via
_resolve_apply_or_force — binding = consent to every future run), the
skill-disabled gate, the key-scope wall on every doc id, and delegation to the
ONE service (pipeline/core/bindings.py) the editor's REST routes also call —
no business logic here.
"""
import asyncio
import json
import time

from agent.context import get_agent_context
from fastapi import Depends, HTTPException
from pipeline.core import bindings
from tiers import require_tier

from access import require_project_full
from db import extract_id, fetch_many, fetch_one, get_db
from models import (
    AgentConfigCreate,
    PipelineScheduleCreate,
    PipelineSchedulePatch,
    ToolBindPipeline,
    ToolListPipelines,
    ToolPipelineRuns,
    ToolRunPipeline,
    ToolUnbindPipeline,
)
from routes.tool_api._common import (
    _refuse_pipelines_disabled,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool
from scope import (
    doc_row_with_trash,
    in_subtree,
    out_of_scope_detail,
    require_doc_in_scope,
    trashed_doc_in_subtree,
)


async def _scope_check(ctx: dict, doc_id: str) -> None:
    """The key's walls on one document id — its PROJECT first, then its subtree;
    one choke point so no handler forgets either.

    # INVARIANT(security): every document id a tool receives must resolve inside
    the KEY's project before any service call. Why: the service's RBAC is the
    OWNING USER's (scope ⊂ RBAC), and a user with full access to projects A and
    B holds it in both — RBAC alone lets a project-A key act on project B. The
    key's project is the wall every other handler binds explicitly
    (structure / subtree / imports), and the subtree check is no project check
    at all: a whole-project key has scope_root '' and `in_subtree('')` is
    always True. A document of another project answers the uniform 404 (no
    existence oracle across projects). The service stays the authority on
    what the user may do INSIDE the project.
    """
    doc = await fetch_one("documents", doc_id)
    if not doc or doc.get("deleted_at") or doc.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Document not found")
    await require_doc_in_scope(ctx.get("scope_root") or "", doc_id)


async def _scoped_binding(ctx: dict, config_id: str) -> dict:
    """Load a binding of the KEY's project (uniform 404 otherwise), scope-check
    its SOURCE document, return the row."""
    row = await fetch_one("agent_configs", config_id)
    if (
        not row or row.get("deleted_at")
        or row.get("project_id") != ctx["project_id"]
    ):
        raise HTTPException(status_code=404, detail="Agent config not found")
    await _scope_check(ctx, extract_id(row.get("document_id")))
    return row


async def _unbindable_binding(ctx: dict, config_id: str) -> None:
    """`_scoped_binding` for the one tool that must reach an ORPHAN: a binding
    whose source document is in the trash stays removable.

    # INVARIANT(security): a trashed source still passes both walls (project: uniform 404; subtree by last position: 403).
    # Why: cascading the binding away on document
    # delete would lose its config on restore, so the agent must be able to
    # clean the orphan up — but a trashed doc is no exemption from the scope.
    """
    row = await fetch_one("agent_configs", config_id)
    if not row or row.get("deleted_at") or row.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Agent config not found")
    source_id = extract_id(row.get("document_id"))
    doc = await doc_row_with_trash(source_id)
    if not doc or doc.get("deleted_at") is None:
        await _scope_check(ctx, source_id)
        return
    if extract_id(doc.get("project_id")) != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Document not found")
    scope_root = ctx.get("scope_root") or ""
    if not await trashed_doc_in_subtree(scope_root, source_id):
        raise HTTPException(status_code=403, detail=out_of_scope_detail(scope_root))


async def _consent(ctx: dict, body, tool_name: str) -> None:
    """The three mutating tools follow tool_save_skill: resolve, refuse
    unconfirmable — the driver's dsh approval round re-sends with the verdict.
    A binding is NOT a system doc (it is project data)."""
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )
    if decision.mode != "auto":
        _refuse_unconfirmable(tool_name, decision.reason)


async def _scoped_binding_rows(
    rows: list, scope_root: str, wanted: str | None,
) -> list:
    """The caller's narrowing (wanted) + the key's subtree wall, per row — a
    handful of rows per project, so one ancestor check per row is acceptable
    and the wall holds on reads too."""
    out = []
    for r in rows or []:
        source_id = extract_id(r.get("document_id"))
        if wanted and source_id != wanted:
            continue
        if scope_root and not (
            await in_subtree(scope_root, source_id)
            or await trashed_doc_in_subtree(scope_root, source_id)
        ):
            continue
        out.append(r)
    return out


async def _schedules_by_config(db, project_id: str) -> dict[str, dict]:
    """The project's live digest schedule rows keyed by their binding's
    config_id (exact params match — the same link the unbind cascade uses)."""
    rows = await db.query(
        "SELECT * FROM pipeline_schedules WHERE project_id = $pid "
        "AND pipeline = 'digest' AND deleted_at IS NONE",
        {"pid": project_id},
    )
    by_config: dict[str, dict] = {}
    for s in rows or []:
        try:
            params = json.loads(s.get("params_json") or "{}")
        except (TypeError, ValueError):
            continue
        if params.get("config_id"):
            by_config[params["config_id"]] = s
    return by_config


async def _last_runs_by_config(db, config_ids: list) -> dict[str, dict]:
    """Each binding's newest recorded run — one indexed (config_id, queued_at)
    LIMIT 1 read per binding.

    WHY per binding, not one project-wide `ORDER BY queued_at DESC LIMIT N`
    scan: a single digest on the 60 s floor pushes N rows in N minutes, so
    every less frequent binding's last run falls outside the window and reads
    as `null` — "never ran" — on the very field the agent diagnoses "why did
    the pipeline not run" from. A handful of bindings per project ⇒ a handful
    of cheap indexed reads.
    """
    last: dict[str, dict] = {}
    for cid in config_ids:
        rows = await db.query(
            "SELECT * FROM pipeline_runs WHERE config_id = $c "
            "ORDER BY queued_at DESC LIMIT 1",
            {"c": cid},
        )
        if rows:
            last[cid] = _run_view(rows[0])
    return last


@track_agent_tool("list_pipelines")
async def tool_list_pipelines(
    body: ToolListPipelines,
    ctx: dict = Depends(get_agent_context),
):
    """Every live extractor/digest binding of the key's project (never
    `conversational` rows), narrowed to the key's scope; each row carries its
    documents by id AND title, its schedule and its last recorded run."""
    await _refuse_pipelines_disabled((await get_db()))
    project_id = ctx["project_id"]
    await require_project_full(project_id, ctx["user"])
    if body.document_id:
        await _scope_check(ctx, body.document_id)

    db = await get_db()
    rows = await db.query(
        "SELECT * FROM agent_configs WHERE project_id = $pid "
        "AND (kind IN ['extractor', 'digest'] OR kind IS NONE) "
        "AND deleted_at IS NONE ORDER BY created_at",
        {"pid": project_id},
    )
    rows = await _scoped_binding_rows(
        rows or [], ctx.get("scope_root") or "",
        body.document_id,
    )

    doc_ids = sorted({
        extract_id(r.get(key))
        for r in rows for key in ("document_id", "config_doc_id", "target_doc_id")
        if extract_id(r.get(key))
    })
    docs = await fetch_many("documents", doc_ids) if doc_ids else {}
    schedule_by_config = await _schedules_by_config(db, project_id)
    last_runs = await _last_runs_by_config(db, [extract_id(r.get("id")) for r in rows])

    return [
        _binding_view(r, docs, schedule_by_config, last_runs) for r in rows
    ]


def _title(docs: dict, doc_id: str | None) -> str | None:
    row = docs.get(extract_id(doc_id) or "") if doc_id else None
    return (row or {}).get("title")


def _binding_view(
    row: dict, docs: dict, schedule_by_config: dict, last_runs: dict,
) -> dict:
    config_id = extract_id(row.get("id"))
    source_id = extract_id(row.get("document_id"))
    config_doc_id = extract_id(row.get("config_doc_id"))
    target_id = extract_id(row.get("target_doc_id"))
    out = {
        "config_id": config_id,
        "kind": row.get("kind") or "extractor",
        "document_id": source_id,
        "document_title": _title(docs, source_id),
        # WHY: `docs` holds live rows only (fetch_many skips the trash), so an
        # absent source is the orphan the agent can still unbind_pipeline.
        "source_deleted": bool(source_id) and source_id not in docs,
        "config_doc_id": config_doc_id,
        "config_doc_title": _title(docs, config_doc_id),
        "target_doc_id": target_id,
        "target_doc_title": _title(docs, target_id),
        "trigger_event": row.get("trigger_event") or "transcription_complete",
        "model": row.get("model"),
        "title_template": row.get("title_template"),
        "last_run": last_runs.get(config_id),
    }
    schedule = schedule_by_config.get(config_id)
    if schedule:
        out["schedule"] = {
            "schedule_id": extract_id(schedule.get("id")),
            "interval_s": schedule.get("interval_s"),
            "enabled": schedule.get("enabled") is True,
            "last_run_at": schedule.get("last_run_at"),
            "last_status": schedule.get("last_status"),
        }
    return out


_WAIT_POLL_S = 2.0


async def _wait_until_idle(db, project_id: str, config_id: str, wait_s: int) -> None:
    """Hold the read until the binding has no queued/running run or `wait_s`
    passes — the agent's way to wait for a run it launched (it has no clock)."""
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        live = await db.query(
            "SELECT id FROM pipeline_runs WHERE project_id = $pid AND config_id = $c "
            "AND status IN ['queued', 'running'] LIMIT 1",
            {"pid": project_id, "c": config_id},
        )
        if not live:
            return
        await asyncio.sleep(_WAIT_POLL_S)


def _run_view(run: dict) -> dict:
    return {
        "run_id": extract_id(run.get("id")) or run.get("id"),
        "kind": run.get("kind"),
        "trigger": run.get("trigger"),
        "status": run.get("status"),
        "error": run.get("error"),
        "output_doc_id": run.get("output_doc_id"),
        "reference_id": run.get("reference_id"),
        "queued_at": run.get("queued_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


@track_agent_tool("bind_pipeline")
async def tool_bind_pipeline(
    body: ToolBindPipeline, ctx: dict = Depends(get_agent_context),
):
    """Create OR re-configure one binding (the editor's upsert key), then upsert
    the digest schedule when asked. The approval card shows the spec
    description: binding X on doc Y runs on every new recording."""
    await _refuse_pipelines_disabled((await get_db()))
    if body.kind == "extractor" and body.schedule is not None:
        raise HTTPException(
            status_code=400,
            detail="An extractor runs per reference — a schedule has no reference to run on",
        )
    for doc_id in (body.document_id, body.config_doc_id, body.target_doc_id):
        await _scope_check(ctx, doc_id)
    if body.schedule is not None and body.schedule.enabled:
        # see SYSTEM: capability-tiers — a schedule that STARTS cost is an 'ai'
        # act (create_schedule always; a re-enable in patch_schedule). Checked
        # BEFORE the binding is written so a tier refusal cannot leave a
        # binding without the schedule one approval card covered; the service
        # re-checks and stays the authority. The residual case — a NEW schedule
        # created PAUSED by a below-tier caller — leaves a binding with no
        # schedule at all, i.e. no cost running.
        await require_tier(ctx["user"], "ai")
    await _consent(ctx, body, "bind_pipeline")

    binding = await bindings.upsert_agent_config(
        AgentConfigCreate(
            document_id=body.document_id,
            kind=body.kind,
            config_doc_id=body.config_doc_id,
            target_doc_id=body.target_doc_id,
            title_template=body.title_template,
            model=body.model,
            subject_user_ids=body.subject_user_ids,
        ),
        ctx["user"],
    )
    config_id = binding["config_id"]
    db = await get_db()
    if body.schedule is not None:
        binding["schedule"] = await _upsert_digest_schedule(
            db, config_id, extract_id(binding["project_id"]), body, ctx,
        )
    return binding


async def _upsert_digest_schedule(db, config_id: str, project_id: str, body, ctx) -> dict:
    """Upsert the ONE schedule row of this digest binding (exact params match —
    the same link the unbind cascade uses); disable-then-enable never duplicates.
    Returns the schedule view the bind response carries."""
    existing = (await _schedules_by_config(db, project_id)).get(config_id)
    create = PipelineScheduleCreate(
        project_id=project_id,
        pipeline="digest",
        interval_s=body.schedule.interval_s,
        params={"config_id": config_id, "project_id": project_id},
        enabled=body.schedule.enabled,
    )
    if existing is None:
        row = await bindings.create_schedule(create, ctx["user"])
    else:
        row = await bindings.patch_schedule(
            extract_id(existing.get("id")),
            PipelineSchedulePatch(
                enabled=body.schedule.enabled, interval_s=body.schedule.interval_s,
            ),
            ctx["user"],
        )
    return {
        "schedule_id": row.get("schedule_id"),
        "interval_s": row.get("interval_s"),
        "enabled": row.get("enabled") is True,
        "last_run_at": row.get("last_run_at"),
        "last_status": row.get("last_status"),
    }


@track_agent_tool("unbind_pipeline")
async def tool_unbind_pipeline(
    body: ToolUnbindPipeline, ctx: dict = Depends(get_agent_context),
):
    """Soft-delete a binding; the service cascades a digest binding's schedule
    rows so nothing keeps firing into a missing binding."""
    await _refuse_pipelines_disabled((await get_db()))
    await _unbindable_binding(ctx, body.config_id)
    await _consent(ctx, body, "unbind_pipeline")
    return await bindings.delete_agent_config(body.config_id, ctx["user"])


@track_agent_tool("run_pipeline")
async def tool_run_pipeline(
    body: ToolRunPipeline, ctx: dict = Depends(get_agent_context),
):
    """One binding, now: digest → run_digest_binding; extractor →
    run_extractor_binding (reference required, scoped). accepted/already_pending
    carries the run id for the pipeline_runs verification."""
    await _refuse_pipelines_disabled((await get_db()))
    binding = await _scoped_binding(ctx, body.config_id)
    if body.reference_id:
        await _scope_check(ctx, body.reference_id)
    await _consent(ctx, body, "run_pipeline")
    kind = binding.get("kind") or "extractor"
    if kind == "digest":
        if body.reference_id:
            raise HTTPException(
                status_code=400,
                detail="A digest runs per binding — omit reference_id",
            )
        return await bindings.run_digest_binding(body.config_id, ctx["user"])
    if not body.reference_id:
        raise HTTPException(
            status_code=400,
            detail="An extractor runs per reference: pass reference_id",
        )
    return await bindings.run_extractor_binding(
        body.config_id, body.reference_id, ctx["user"],
    )


@track_agent_tool("pipeline_runs")
async def tool_pipeline_runs(
    body: ToolPipelineRuns,
    ctx: dict = Depends(get_agent_context),
):
    """The run log, newest first — the verification read for what the agent
    bound or launched. Scoped like list_pipelines: an explicit config_id's
    binding must be in scope; unscoped runs cover only in-scope bindings."""
    await _refuse_pipelines_disabled((await get_db()))
    project_id = ctx["project_id"]
    await require_project_full(project_id, ctx["user"])
    limit = body.limit
    db = await get_db()
    scope_root = ctx.get("scope_root") or ""

    if body.config_id:
        await _scoped_binding(ctx, body.config_id)
        await _wait_until_idle(db, project_id, body.config_id, body.wait_s)
        rows = await db.query(
            "SELECT * FROM pipeline_runs WHERE project_id = $pid AND config_id = $c "
            "ORDER BY queued_at DESC LIMIT $limit",
            {"pid": project_id, "c": body.config_id, "limit": limit},
        )
        return [_run_view(r) for r in (rows or [])]

    # No config_id: newest runs across the IN-SCOPE bindings only. Runs with
    # no binding (config_id NONE — a REST-era extractor schedule) are not
    # attributable to a scope and stay invisible to the tool.
    bindings_rows = await db.query(
        "SELECT id, document_id FROM agent_configs WHERE project_id = $pid "
        "AND (kind IN ['extractor', 'digest'] OR kind IS NONE) "
        "AND deleted_at IS NONE",
        {"pid": project_id},
    )
    in_scope_ids = []
    for r in bindings_rows or []:
        doc_id = extract_id(r.get("document_id"))
        if not scope_root or await in_subtree(scope_root, doc_id):
            in_scope_ids.append(extract_id(r.get("id")))
    if not in_scope_ids:
        return []
    rows = await db.query(
        "SELECT * FROM pipeline_runs WHERE project_id = $pid AND config_id IN $ids "
        "ORDER BY queued_at DESC LIMIT $limit",
        {"pid": project_id, "ids": in_scope_ids, "limit": limit},
    )
    return [_run_view(r) for r in (rows or [])]
