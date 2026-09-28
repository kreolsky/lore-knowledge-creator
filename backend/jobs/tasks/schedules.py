"""Project-scoped pipeline schedules — the ONE cron tick (S3).

# see SYSTEM: jobs, SYSTEM: extractor (the scheduled entry).
"""
from __future__ import annotations

import logging

from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)

_DUE_PREDICATE = (
    "enabled = true AND deleted_at IS NONE "
    "AND (last_run_at IS NONE OR last_run_at + 1s * interval_s <= time::now())"
)


async def _claim_due_schedule(db, schedule_id: str) -> str | None:
    """Claim one due schedule; return the claimed-at marker, or None when the
    claim was lost (another tick) or the row is no longer due/enabled.

    The conditional UPDATE's WHERE re-checks the due predicate — CAS by
    # INVARIANT (claim-before-dispatch): the claim races safely because the
    # loser's UPDATE matches a row whose last_run_at the winner just advanced.
    # Why: the arq cron Redis lock alone does not cover a manual tick overlapping
    # the cron tick; the claim is the actual single-dispatch guarantee.
    """
    claimed = await db.query(
        "UPDATE type::record('pipeline_schedules', $id) SET "
        "last_run_at = time::now(), last_status = 'running', "
        "last_error = NONE, updated_at = time::now() "
        f"WHERE {_DUE_PREDICATE} RETURN AFTER",
        {"id": schedule_id},
    )
    if not claimed:
        return None
    return str((claimed[0] or {}).get("last_run_at") or schedule_id)


async def _dispatch_claimed_schedule(db, schedule_id: str, row: dict, claimed_at: str) -> None:
    """Resolve the registry row and enqueue with the frozen params; record the
    dispatch outcome ('ok'/'error') on the schedule row. Raises never — the
    caller's no-wedge invariant depends on this absorbing per-row failures."""
    import json as _json

    from pipeline.core.registry import get_pipeline

    try:
        spec = get_pipeline(str(row.get("pipeline") or ""))
        if spec is None:
            raise ValueError(f"unknown pipeline {row.get('pipeline')!r}")
        params = _json.loads(row.get("params_json") or "{}")
        if not isinstance(params, dict):
            raise ValueError("params_json is not a JSON object")
        await _rebind_scoped_params(db, spec, params, str(row.get("project_id") or ""))

        await jobs_pool.enqueue(
            spec.entry.__name__,
            # Per-claim job_id: enqueue-level idempotency — a replayed claim
            # (crash between claim and enqueue) is a NEW claim anyway; a
            # duplicated tick dispatch is stopped by the claim itself.
            job_id=f"psched:{schedule_id}:{claimed_at}",
            queue=None if spec.queue == "default" else jobs_pool.TRANSCRIPTION_QUEUE,
            **params,
        )
        await db.query(
            "UPDATE type::record('pipeline_schedules', $id) SET "
            "last_status = 'ok', last_error = NONE, updated_at = time::now()",
            {"id": schedule_id},
        )
    except Exception as e:  # noqa: BLE001 — record and continue (no wedge)
        logger.error("pipeline schedule %s dispatch failed: %s", schedule_id, e)
        await db.query(
            "UPDATE type::record('pipeline_schedules', $id) SET "
            "last_status = 'error', last_error = $err, updated_at = time::now()",
            {"id": schedule_id, "err": str(e)[:500]},
        )


async def _rebind_scoped_params(db, spec, params: dict, project_id: str) -> None:
    """Claim-time re-binding of the project-scoped params — the defense in depth
    behind create-side validation (a hand-edited row, or a params blob written by
    a deploy window without create validation).

    # INVARIANT(security): the row's project is the authority — params['project_id']
    # is FORCE-overridden from the row; foreign id-params refuse the run.
    # Why: the worker has no user context to re-run the interactive triggers'
    # per-call access checks; without re-binding, a frozen-params schedule is the
    # one extractor entry point that can read and write across project walls.
    """
    from db import fetch_many

    scoped = spec.project_scoped_params
    if not scoped:
        return
    if not project_id:
        raise ValueError("schedule row has no project_id")
    if "project_id" in scoped:
        params["project_id"] = project_id
    id_params = [str(params[k]) for k in scoped if k != "project_id" and params.get(k)]
    docs = await fetch_many("documents", id_params) if id_params else {}
    for key in scoped:
        if key == "project_id" or not params.get(key):
            continue
        doc = docs.get(str(params[key]))
        if doc is None or doc.get("project_id") != project_id:
            raise ValueError(
                f"params['{key}'] does not resolve to a document in the "
                f"schedule's project"
            )


async def pipeline_schedule_tick_task(ctx) -> None:
    """The ONE cron tick that claims and dispatches due pipeline_schedules rows.

    # WHY: schedules are project data — this tick is the single dispatch path, so
    # adding a scheduled pipeline never touches WorkerSettings and the four
    # hardcoded maintenance cron_jobs stay untouched.

    # INVARIANT (no wedge): each row is claimed and dispatched inside its own
    # absorbing failure path (_dispatch_claimed_schedule); a failing schedule
    # records last_status='error' and the tick continues. last_run_at advances
    # AT CLAIM regardless of outcome, so a failing schedule becomes due again
    # interval_s later and never blocks its own next run or the tick.
    # Why: a schedule on a removed pipeline must not take the whole scheduler
    # down with it — the plan's acceptance pins the sibling row still firing.
    """
    from db import extract_id, get_db

    db = await get_db()
    # Bounded per tick (memory discipline); the rest is picked up next tick.
    rows = await db.query(
        "SELECT id, created_at, project_id, pipeline, params_json "
        f"FROM pipeline_schedules WHERE {_DUE_PREDICATE} "
        "ORDER BY created_at LIMIT 50",
    )
    for row in rows or []:
        schedule_id = extract_id(row["id"])
        claimed_at = await _claim_due_schedule(db, schedule_id)
        if claimed_at is None:
            continue
        await _dispatch_claimed_schedule(db, schedule_id, row, claimed_at)
