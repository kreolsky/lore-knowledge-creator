"""Pipeline run log — the ONE writer of pipeline_runs rows.

Every enqueue site of extract_task / digest_task posts through `launch`, which
records `queued` BEFORE the post and discards the row when arq did not land the
job (enqueue returns None on a job_id collapse — a collapsed post is not a run);
the two tasks mark `running` / `finished` around their existing try/except. A task invoked without a run_id (in-flight
jobs at deploy time) records nothing and must not crash.

# ARCH: best-effort by contract — a pipeline_runs write failure logs and never
# raises. Why: the run log is diagnostics; failing a real extraction (or 500ing
# a REST run request whose job was ALREADY posted to Redis) over a lost
# telemetry row inverts the value of the thing being logged. The pipelines'
# real failure traces (error notes) stay on their own paths.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from db import get_db

logger = logging.getLogger(__name__)


def new_run_id() -> str:
    """The pipeline_runs row id a dispatch will record under."""
    return str(uuid4())


async def record_queued(
    *,
    run_id: str,
    project_id: str,
    kind: str,
    trigger: str,
    config_id: str | None = None,
    reference_id: str | None = None,
) -> None:
    """Write the `queued` row (callers post through `launch`)."""
    try:
        db = await get_db()
        await db.query(
            "CREATE type::record('pipeline_runs', $id) SET "
            "project_id = $pid, config_id = $cfg, kind = $kind, "
            "reference_id = $ref, trigger = $trigger, status = 'queued'",
            {
                "id": run_id, "pid": project_id, "cfg": config_id,
                "kind": kind, "ref": reference_id, "trigger": trigger,
            },
            site="pipeline_runs_record_queued",
        )
    except Exception:
        logger.warning("pipeline_runs queued write failed for %s", run_id, exc_info=True)


async def discard(run_id: str) -> None:
    """Remove a `queued` row whose post did not land (collapse or enqueue error)."""
    try:
        db = await get_db()
        await db.query(
            "DELETE type::record('pipeline_runs', $id)", {"id": run_id},
            site="pipeline_runs_discard",
        )
    except Exception:
        logger.warning("pipeline_runs discard failed for %s", run_id, exc_info=True)


async def launch(
    post: Callable[[str], Awaitable[object]],
    *,
    project_id: str,
    kind: str,
    trigger: str,
    config_id: str | None = None,
    reference_id: str | None = None,
) -> str | None:
    """Record a run, then post its job via `post(run_id)`; returns the run id,
    or None when arq collapsed the post onto a pending job of the same id."""
    run_id = new_run_id()
    # WHY the `queued` row is written BEFORE the job is posted: the worker can
    # pick the job up before the web process's next statement; its
    # mark_running / mark_finished are UPDATEs, which on a missing record
    # create nothing, so a row written after the post could stay `queued`
    # forever and be reported as a pending run.
    await record_queued(
        run_id=run_id, project_id=project_id, kind=kind, trigger=trigger,
        config_id=config_id, reference_id=reference_id,
    )
    try:
        job = await post(run_id)
    except BaseException:
        await discard(run_id)
        raise
    if job is None:
        await discard(run_id)
        return None
    return run_id


async def mark_running(run_id: str) -> None:
    try:
        db = await get_db()
        await db.query(
            "UPDATE type::record('pipeline_runs', $id) SET "
            "status = 'running', started_at = time::now()",
            {"id": run_id},
            site="pipeline_runs_mark_running",
        )
    except Exception:
        logger.warning("pipeline_runs running mark failed for %s", run_id, exc_info=True)


async def mark_finished(
    run_id: str,
    status: str,
    *,
    error: str | None = None,
    output_doc_id: str | None = None,
) -> None:
    """Terminal mark: `ok` / `skipped` / `error` (+ the run's output document)."""
    try:
        db = await get_db()
        await db.query(
            "UPDATE type::record('pipeline_runs', $id) SET "
            "status = $status, error = $err, output_doc_id = $out, "
            "finished_at = time::now()",
            {"id": run_id, "status": status, "err": error, "out": output_doc_id},
            site="pipeline_runs_mark_finished",
        )
    except Exception:
        logger.warning("pipeline_runs finished mark failed for %s", run_id, exc_info=True)


__all__ = ["discard", "launch", "mark_finished", "mark_running", "new_run_id", "record_queued"]
