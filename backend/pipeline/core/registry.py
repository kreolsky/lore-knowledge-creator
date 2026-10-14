"""Pipeline registry — one declarative row per pipeline job.

# SYSTEM: pipeline-registry — the catalog of pipeline jobs (name → entry + queue +
# IO descriptors) that schedules (S3) and future pipelines address.

# ARCH: a row describes the ARQ TASK — the enqueue address — not the PocketFlow
# flow inside it. Why: the task is what a schedule enqueues and what the worker
# registers; the flow is an internal of the extractor task and a future pipeline
# may have none (a pure maintenance job). The registry adds NO dispatch
# indirection: callers keep enqueueing the arq task directly (the extractor's
# three trigger paths are unchanged); the row is addressable metadata + totality
# guards.

# INVARIANT (no silent drop): every row's task MUST be registered on the declared
# queue's worker class. Why: arq silently drops a job whose function is not on the
# polled worker — a registered-less row would enqueue and never run. The check
# runs at worker boot (jobs.worker.validate_queue_config) and in CI
# (tests/backend/test_pipeline_registry.py), mirroring BACKUP_TASK_NAMES /
# COMFY_TASK_NAMES. The reverse direction (every pipeline package's PIPELINE_TASKS
# export has a row) is bound by the test's derived package walk — no phantom rows,
# no forgotten pipelines.
"""
from dataclasses import dataclass
from typing import Callable

from db import extract_id, fetch_many
from jobs.tasks.digest import digest_job_id, digest_task
from jobs.tasks.extract import extract_task
from jobs.tasks.widget_extract import widget_extract_task

# The queues a pipeline row may declare. Keys mirror the two worker classes in
# jobs/worker.py; a row declares WHERE its task is registered, not a transport.
KNOWN_QUEUES = frozenset({"default", "transcription"})


@dataclass(frozen=True)
class PipelineSpec:
    """One pipeline job declaration.

    name:           the registry key — the addressable pipeline name (schedules
                    address this, never the task name).
    entry:          the arq-registered coroutine. Its __name__ is the enqueue
                    address and MUST appear in the declared queue's worker
                    functions (validate_registry).
    queue:          which worker class runs it ("default" | "transcription").
    params_source:  how the task's arguments are resolved (descriptor — a human
                    answer to "where do the inputs come from").
    result_target:  where the run's artifact lands (descriptor — same discipline).
    project_scoped_params: the task-kwargs names whose values are bound to ONE
                    project — "project_id" (must equal the schedule's project)
                    plus every record-id param that must resolve inside it.
                    The schedule CRUD validates at create and the tick re-binds
                    at claim. Why: the worker has no user context to re-run the
                    per-call access checks the interactive triggers have; without
                    this declaration a frozen-params schedule is the one extractor
                    entry point that can read/write across project walls.
                    An id-param is a document id unless SCOPED_PARAM_TABLES says
                    otherwise.
    job_id:         the arq job id a scheduled run posts under, from its params.
                    None → a per-claim id. Set it when the task's other triggers
                    collapse on a stable id, so a scheduled run collapses with them.
    """

    name: str
    entry: Callable
    queue: str
    params_source: str
    result_target: str
    project_scoped_params: tuple[str, ...] = ()
    job_id: Callable[[dict], str] | None = None


PIPELINES: tuple[PipelineSpec, ...] = (
    PipelineSpec(
        name="extractor",
        entry=extract_task,
        queue="default",
        params_source=(
            "agent_configs row bound to the source document — resolved by "
            "resolve_extractor_params (manual/MCP) or on_transcription_complete "
            "(auto trigger); both feed the same extract_task"
        ),
        result_target=(
            "a child document created under target_doc_id (run_extractor → "
            "create_document + document_created emit); failures dead-letter as a "
            "Pipeline Error system note"
        ),
        project_scoped_params=(
            "project_id", "reference_id", "source_doc_id",
            "config_doc_id", "target_doc_id",
        ),
    ),
    # The widget batch API's dry entry (CIR T2, plan widget-extract-batch-api):
    # STT + run_extractor_dry in one task; the result lands on the SOURCE
    # reference's file_meta.extract, never a document.
    PipelineSpec(
        name="extractor_dry_widget",
        entry=widget_extract_task,
        queue="default",
        params_source=(
            "resolved in POST /api/widget/extract via resolve_extractor_params "
            "(require_project_full on the key-owning user), frozen into the "
            "job kwargs"
        ),
        result_target=(
            "file_meta.extract on the source reference; no document — the dry "
            "extractor path, polled via GET /api/widget/extract/{job_id}"
        ),
        project_scoped_params=(
            "project_id", "reference_id", "source_doc_id",
            "config_doc_id", "target_doc_id",
        ),
    ),
    PipelineSpec(
        name="digest",
        entry=digest_task,
        queue="default",
        params_source=(
            "a digest agent_configs row (config_id) — the task loads it and "
            "re-checks its subjects against project access at run time"
        ),
        result_target=(
            "a child document under the binding's target_doc_id carrying "
            "digest_config_id + digest_covered_until (the next run's cursor); "
            "nothing when there is no new material; failures dead-letter as a "
            "Pipeline Error note on the theme document"
        ),
        project_scoped_params=("project_id", "config_id"),
        job_id=lambda params: digest_job_id(params["config_id"]),
    ),
)

# The scoped id-params that name a record of a table other than `documents`.
SCOPED_PARAM_TABLES = {"config_id": "agent_configs"}


async def foreign_scoped_param(spec: PipelineSpec, params: dict, project_id: str) -> str | None:
    """The first declared id-param of `params` that does not resolve to a live
    record of `project_id` (each in its own table), or None when all do."""
    keys = [k for k in spec.project_scoped_params if k != "project_id" and params.get(k)]
    ids_by_table: dict[str, list[str]] = {}
    for key in keys:
        ids_by_table.setdefault(SCOPED_PARAM_TABLES.get(key, "documents"), []).append(str(params[key]))
    rows = {table: await fetch_many(table, ids) for table, ids in ids_by_table.items()}
    for key in keys:
        row = rows[SCOPED_PARAM_TABLES.get(key, "documents")].get(str(params[key]))
        if row is None or extract_id(row.get("project_id")) != project_id:
            return key
    return None


def pipeline_names() -> list[str]:
    """The addressable pipeline names (registry keys)."""
    return [row.name for row in PIPELINES]


def get_pipeline(name: str) -> PipelineSpec | None:
    """Look one pipeline up by its registry key."""
    return next((row for row in PIPELINES if row.name == name), None)


def validate_registry(
    rows: tuple[PipelineSpec, ...] | list[PipelineSpec] | None = None,
    *,
    default_fn_names: set[str] | None = None,
    transcription_fn_names: set[str] | None = None,
) -> None:
    """Assert every row is well-formed and its task is registered on the declared
    queue's worker.

    Raises AssertionError naming the offending row (fail loud at worker boot / CI).
    The fn-name sets are injected so the pure checks are testable with synthetic
    rows; the boot gate passes the DERIVED sets from the worker classes.
    """
    if rows is None:
        rows = PIPELINES
    seen: set[str] = set()
    for row in rows:
        task = getattr(row.entry, "__name__", None)
        if row.name in seen:
            raise AssertionError(f"pipeline registry has duplicate name: {row.name!r}")
        seen.add(row.name)
        if row.queue not in KNOWN_QUEUES:
            raise AssertionError(
                f"pipeline {row.name!r} declares unknown queue {row.queue!r} "
                f"(known: {sorted(KNOWN_QUEUES)})"
            )
        if not callable(row.entry) or not task:
            raise AssertionError(f"pipeline {row.name!r} has no callable entry")
        if not (row.params_source or "").strip():
            raise AssertionError(f"pipeline {row.name!r} has a blank params_source")
        if not (row.result_target or "").strip():
            raise AssertionError(f"pipeline {row.name!r} has a blank result_target")
        registered = (
            default_fn_names if row.queue == "default" else transcription_fn_names
        )
        if task not in (registered or ()):
            raise AssertionError(
                f"pipeline {row.name!r} names task {task!r} which is NOT registered "
                f"on the {row.queue!r} worker — it would enqueue and never run "
                f"(arq silent drop)"
            )


__all__ = [
    "PIPELINES",
    "PipelineSpec",
    "KNOWN_QUEUES",
    "SCOPED_PARAM_TABLES",
    "foreign_scoped_param",
    "pipeline_names",
    "get_pipeline",
    "validate_registry",
]
