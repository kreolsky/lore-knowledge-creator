"""Project-scoped pipeline schedules — CRUD.

# SYSTEM: pipeline-schedules — user-configurable scheduled pipeline runs as
# project data; ONE worker cron tick (jobs.tasks.pipeline_schedule_tick_task)
# claims and dispatches due entries.

# ARCH: schedules are project DATA, not code — the worker's ONE cron tick
# reads due rows, so adding a scheduled pipeline never touches WorkerSettings and
# the four hardcoded maintenance cron_jobs stay untouched. Pipeline names are
# validated against the REGISTRY (pipeline/core/registry.py), never a hand-list.

The routes only create/patch/delete rows; nothing here dispatches. The tick is
the single dispatch path, with the claim-before-dispatch invariant (see
jobs.tasks.pipeline_schedule_tick_task).
"""
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pipeline.core.registry import pipeline_names
from surrealdb import AsyncSurreal

from access import require_project_full
from auth import get_current_user
from db import create_record, fetch_one, get_db, serialize_record, soft_delete
from models import PipelineScheduleCreate, PipelineSchedulePatch

router = APIRouter(prefix="/api")


async def _load_owned_schedule(schedule_id: str, user: dict) -> dict:
    """Load a schedule and enforce full access on ITS project — uniform 404 on
    missing/soft-deleted/cross-project ids (no existence oracle), the
    load_owned_proposal discipline."""
    row = await fetch_one("pipeline_schedules", schedule_id)
    if not row or row.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Schedule not found")
    await require_project_full(row.get("project_id", ""), user)
    return row


def _serialize(row: dict) -> dict:
    out = serialize_record(row, "schedule_id")
    # params_json → params (parsed; a corrupt blob surfaces as {} — the row was
    # validated at create, and the tick re-guards with its own error recording).
    try:
        out["params"] = json.loads(out.pop("params_json", "") or "{}")
    except (TypeError, ValueError):
        out["params"] = {}
    return out


@router.get("/pipeline-schedules")
async def list_schedules(
    project_id: str, user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    await require_project_full(project_id, user)
    rows = await db.query(
        "SELECT * FROM pipeline_schedules "
        "WHERE project_id = $pid AND deleted_at IS NONE ORDER BY created_at DESC",
        {"pid": project_id},
    )
    return [_serialize(r) for r in (rows or [])]


async def _validate_schedule_params(spec, params: dict, project_id: str) -> None:
    """Reject params that cannot dispatch safely — at CREATE, where the caller
    still gets an actionable 400 instead of a schedule that errors every tick.

    # INVARIANT(security): the frozen params are the kwargs the worker runs
    # with NO user context — this gate substitutes per-call access checks.
    # Why: a full-access member of project A must not schedule a run whose ids
    # point into project B (the extractor reads the source and writes the child
    # document wherever the params say). Binds the signature (no foreign/reserved
    keys), binds params["project_id"] to the schedule's project, and resolves
    every declared id-param inside that project.
    """
    import inspect

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
    from db import fetch_many

    id_params = [str(params[k]) for k in scoped if k != "project_id" and params.get(k)]
    if id_params:
        docs = await fetch_many("documents", id_params)
        for key in scoped:
            if key == "project_id" or not params.get(key):
                continue
            doc = docs.get(str(params[key]))
            if doc is None or doc.get("project_id") != project_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"params['{key}'] does not resolve to a document in this project",
                )


@router.post("/pipeline-schedules")
async def create_schedule(
    body: PipelineScheduleCreate, user: dict = Depends(get_current_user),
):
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
    schedule_id = str(uuid4())
    record = await create_record("pipeline_schedules", schedule_id, {
        "project_id": body.project_id,
        "pipeline": body.pipeline,
        "interval_s": body.interval_s,
        "enabled": body.enabled,
        "params_json": json.dumps(body.params),
        "created_by": user.get("user_id", ""),
    })
    return _serialize(record)


@router.patch("/pipeline-schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: str, body: PipelineSchedulePatch,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    await _load_owned_schedule(schedule_id, user)
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
    return _serialize(row)


@router.delete("/pipeline-schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str, user: dict = Depends(get_current_user),
):
    await _load_owned_schedule(schedule_id, user)
    await soft_delete("pipeline_schedules", schedule_id)
    return {"success": True}
