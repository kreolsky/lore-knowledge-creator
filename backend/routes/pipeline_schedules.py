"""Project-scoped pipeline schedules — CRUD.

# SYSTEM: pipeline-schedules — user-configurable scheduled pipeline runs as
# project data; ONE worker cron tick (jobs.tasks.pipeline_schedule_tick_task)
# claims and dispatches due entries.

# ARCH: schedules are project DATA, not code — the worker's ONE cron tick
# reads due rows, so adding a scheduled pipeline never touches WorkerSettings and
# the four hardcoded maintenance cron_jobs stay untouched. Pipeline names are
# validated against the REGISTRY (pipeline/core/registry.py), never a hand-list.

The CRUD bodies live in pipeline/core/bindings.py (ONE body, two callers: this
editor surface and the agent's tool-api handlers); nothing here dispatches. The
tick is the single dispatch path, with the claim-before-dispatch invariant (see
jobs.tasks.pipeline_schedule_tick_task).
"""
from fastapi import APIRouter, Depends
from pipeline.core import bindings
from surrealdb import AsyncSurreal

from access import require_project_full
from auth import get_current_user
from db import get_db
from models import PipelineScheduleCreate, PipelineSchedulePatch

router = APIRouter(prefix="/api")


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
    return [bindings.serialize_schedule(r) for r in (rows or [])]


@router.post("/pipeline-schedules")
async def create_schedule(
    body: PipelineScheduleCreate, user: dict = Depends(get_current_user),
):
    return await bindings.create_schedule(body, user)


@router.patch("/pipeline-schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: str, body: PipelineSchedulePatch,
    user: dict = Depends(get_current_user),
):
    return await bindings.patch_schedule(schedule_id, body, user)


@router.delete("/pipeline-schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str, user: dict = Depends(get_current_user),
):
    return await bindings.delete_schedule(schedule_id, user)
