"""arq worker settings — two worker classes for job isolation.

# ARCH: Two separate worker processes:
#   WorkerSettings (default queue): extractor, embeddings, auto_backup, thumbnails.
#   TranscriptionWorkerSettings (transcription queue, max_jobs=STT_CONCURRENCY):
#       only transcribe_task — bounds global STT concurrency.
# INVARIANT: max_jobs is per-process. Scaling TranscriptionWorkerSettings replicas
#            would multiply the effective STT cap — keep replicas: 1.  Why: max_jobs bounds STT concurrency per process; N replicas would run N× the cap and oversubscribe the provider, so the transcription worker stays single-replica.
"""

import logging

import http_clients
from arq import cron, func
from arq.connections import RedisSettings
from code_stamp import stale_code_guard

import config

# ARCH: import `embeddings` at worker startup so its module-level
# `_bus_on("content_flushed", _on_content_flushed)` is subscribed from process start.
# The worker emits content_flushed at its write sites (tasks.py: D1b), and the
# scheduler that turns it into an embed_document_task lives in embeddings.py — an
# unimported module is an unsubscribed bus, so a worker write would emit to nobody
# and the embed would never be scheduled. This is the WORKER process's load-bearing
# import: the web process has its own twin in main.py (the jobs package facade was
# emptied, so nothing in the web process reaches this module any more). Do not delete
# this one because "main.py already imports embeddings" — the worker process never
# imports main. Restart the worker after touching it (arq does not hot-reload).
import embeddings  # noqa: F401 — load-bearing bus-subscription import (see above)
from jobs.pool import TRANSCRIPTION_QUEUE
from jobs.tasks import (
    EMBED_MAX_TRIES,
    auto_backup_handoff_task,
    auto_backup_last_session_task,
    auto_backup_loss_task,
    auto_backup_open_task,
    backfill_cp_blobs_task,
    convert_docx_task,
    convert_pdf_task,
    embed_document_task,
    embed_sweep_task,
    extract_task,
    generate_image_task,
    help_seed_task,
    help_sweep_task,
    nullout_inline_content_task,
    pipeline_schedule_tick_task,
    telemetry_retention_task,
    thin_auto_checkpoints_task,
    thumbnail_task,
    transcribe_task,
    widget_extract_task,
)

logger = logging.getLogger(__name__)


def validate_queue_config() -> None:
    """Assert worker queue_name matches enqueue-site constants.

    Called at worker startup to prevent silent "enqueued but never consumed"
    failures. Why: arq silently drops jobs when the enqueue queue_name !=
    worker queue_name — no error, no retry, the job just sits forever.
    """
    assert TranscriptionWorkerSettings.queue_name == TRANSCRIPTION_QUEUE, (
        f"TranscriptionWorkerSettings.queue_name={TranscriptionWorkerSettings.queue_name!r} "
        f"!= TRANSCRIPTION_QUEUE={TRANSCRIPTION_QUEUE!r}"
    )
    # The two worker classes must poll DIFFERENT queues. If both bound the same queue,
    # one class's jobs would be consumed (or starved) by the other despite disjoint
    # function sets — the same "enqueued but never run for THIS worker" silent failure.
    # WorkerSettings has no queue_name → arq's default 'arq:queue', which must not collide
    # with TRANSCRIPTION_QUEUE. (This check is non-tautological, unlike the constant-equality
    # assert above which only guards the constant from being re-hardcoded to a literal.)
    default_queue = getattr(WorkerSettings, "queue_name", None)
    assert default_queue != TranscriptionWorkerSettings.queue_name, (
        f"Both worker classes poll the same queue ({TranscriptionWorkerSettings.queue_name!r}) — "
        f"one class's jobs will be starved. Give the default worker a distinct queue_name."
    )
    trans_fn_names = {getattr(f, "name", None) for f in TranscriptionWorkerSettings.functions}
    default_fn_names = {getattr(f, "name", None) for f in WorkerSettings.functions}
    overlap = trans_fn_names & default_fn_names
    assert not overlap, f"Functions registered in both workers: {overlap}"

    # Registered-task completeness (backup + Comfy image-gen) lives in the helper so
    # this gate function stays under the func-length limit. See
    # _assert_required_tasks_registered for the per-family Why:.
    _assert_required_tasks_registered(default_fn_names)
    _assert_registry_rows_registered(default_fn_names, trans_fn_names)


def _assert_registry_rows_registered(
    default_fn_names: set[str], transcription_fn_names: set[str],
) -> None:
    """Every pipeline-registry row's task MUST be registered on its declared queue.

    # WHY: the registry (pipeline/core/registry.py) is what schedules
    # address pipelines by; an unregistered row enqueues and never runs.
    # Why: validated at boot because that is the last moment a deploy still has a
    # human reading the log; after boot the failure is invisible (arq's silent
    # drop — a schedule that never fires, with no error anywhere). Lazy import:
    # registry imports jobs.tasks for its rows; worker.py must not grow a
    # top-level pipeline dependency."""
    from pipeline.core.registry import PIPELINES, validate_registry

    validate_registry(
        rows=PIPELINES,
        default_fn_names=default_fn_names,
        transcription_fn_names=transcription_fn_names,
    )


def _assert_required_tasks_registered(default_fn_names: set[str]) -> None:
    """Every task family the launcher enqueues MUST be registered on the default worker.

    Shared failure mode: arq silently drops a job whose function is not on the polled
    worker, so a declared-but-unregistered task enqueues and never runs (the safety
    backup never happens; the image-gen spinner never clears). The canonical sets live
    in jobs.tasks (BACKUP_TASK_NAMES, COMFY_TASK_NAMES) — a task forgotten in
    WorkerSettings.functions crashes the worker at boot here, not silently in prod."""
    from jobs.tasks import BACKUP_TASK_NAMES, COMFY_TASK_NAMES

    # INVARIANT: every declared backup task MUST be registered in
    # WorkerSettings.functions. Why: a backup task enqueued via enqueue(...) but not
    # registered on the default worker silently never runs — no error, no retry, the
    # safety backup just never happens. Canonical set: jobs.tasks.BACKUP_TASK_NAMES.
    missing = BACKUP_TASK_NAMES - default_fn_names
    assert not missing, (
        f"Backup tasks declared but not registered in WorkerSettings.functions: {missing} — "
        f"they would enqueue but never run."
    )

    # INVARIANT: every Comfy image-gen task MUST be registered in
    # WorkerSettings.functions. Why: the tool launcher enqueues generate_image_task; a
    # task registered nowhere is silently dropped by arq — the launcher returns
    # {status:"generating"} and the spinner never clears. Canonical set:
    # jobs.tasks.COMFY_TASK_NAMES.
    comfy_missing = COMFY_TASK_NAMES - default_fn_names
    assert not comfy_missing, (
        f"Comfy image-gen tasks declared but not registered in "
        f"WorkerSettings.functions: {comfy_missing} — they would enqueue but never run."
    )


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(config.REDIS_URL)


def _guarded(raw_funcs: list) -> list:
    """Wrap each task coroutine with the stale-code guard.

    # ARCH: arq 0.28 has no Worker.middleware, and raising in on_job_start crashes the
    #       worker (run_job exceptions re-raise via main()'s t.result()). Wrapping the
    #       coroutine makes the refusal a normal arq job failure (retry-then-fail), not
    #       a worker crash. func() derives the registered name from the coroutine's
    #       __qualname__, which functools.wraps preserves, so validate_queue_config's
    #       name checks and the enqueue-site function names are unaffected.
    Applied to BOTH worker classes so a divergence refuses every user-facing job; cron
    jobs are unguarded (idempotent maintenance)."""
    return [func(stale_code_guard(f.coroutine), max_tries=f.max_tries) for f in raw_funcs]


async def _on_worker_startup(ctx) -> None:
    validate_queue_config()
    # SYSTEM: instance-settings — this worker's override-cache drop. off+on so a
    # watchfiles reload never stacks duplicate registrations. ORDER MATTERS: the
    # registration must precede subscribe_backplane_events(), whose subscribe loop
    # walks only locally-registered event types — registered after, the channel
    # is skipped and web-side PUTs never invalidate this process's cache.
    import settings

    from event_bus import off as _bus_off
    from event_bus import on as _bus_on
    from event_bus import subscribe_backplane_events
    _bus_off(settings.SETTINGS_EVENT, settings.drop_cache)
    _bus_on(settings.SETTINGS_EVENT, settings.drop_cache)
    await subscribe_backplane_events()
    # Warm the shared transcription client (this is the STT worker process —
    # SYSTEM: http-clients).
    import transcription
    http_clients.get_http_client("transcription", timeout=transcription.STT_TIMEOUT_S)
    # Publish this worker's code stamp so the web process can flag a stale worker
    # (forgot-to-rebuild foot-gun) on /api/health. Best-effort: a failure here must
    # never block the worker from starting.
    try:
        from code_stamp import CODE_STAMP, WORKER_STAMP_REDIS_KEY
        await ctx["redis"].set(WORKER_STAMP_REDIS_KEY, CODE_STAMP)
        logger.info("arq worker started (code_stamp=%s)", CODE_STAMP)
    except Exception:
        logger.warning("Failed to publish worker code_stamp", exc_info=True)


async def _on_worker_shutdown(ctx) -> None:
    # Close every pooled httpx client this worker built (transcription,
    # embeddings, media — SYSTEM: http-clients) in one place.
    await http_clients.close_all()
    logger.info("arq worker stopped")


# WHY: keep_result=0 on both workers — a stable job_id (embed:/transcribe:/
# extract:/thumb:/backup:…) MUST be reusable immediately after a run for re-trigger and
# trailing debounce. Why: arq's enqueue_job dedups on arq:result:{id} as well as the job
# key, so ANY result retention silently refuses the next enqueue for that window.
# Failures are recorded in the DB (entity status + *_error events / error notes), not
# via arq result retention. NOTE: the arq Worker setting is `keep_result` (seconds), NOT
# `keep_result_s` — get_kwargs only forwards names matching the Worker signature, so a
# misnamed `keep_result_s` is silently dropped and arq falls back to its 3600s default.
class WorkerSettings:
    redis_settings = _redis_settings()
    on_startup = _on_worker_startup
    on_shutdown = _on_worker_shutdown
    retry_jobs = True
    keep_result = 0

    functions = _guarded([
        func(extract_task, max_tries=4),
        # WHY(EMBED_MAX_TRIES, one constant): the task dead-letters on its final
        # attempt by comparing ctx["job_try"] against the SAME constant (see
        # jobs/tasks/embed.py) — a literal here that drifted from the task's
        # constant would send the last attempt into arq's silent terminal path
        # (job_try > max_tries aborts WITHOUT invoking the task: no recorder,
        # no 'failed' status — the 187-doc prod hole of 2026-08-18).
        func(embed_document_task, max_tries=EMBED_MAX_TRIES),
        # Hourly coverage sweep, ALSO manually enqueueable (the prod backfill
        # path). max_tries=1: idempotent — a failed run is re-run by the next
        # cron slot; a retry storm adds nothing.
        func(embed_sweep_task, max_tries=1),
        func(thumbnail_task, max_tries=4),
        # Lore guide: idempotent (deterministic ids), so a retry is safe.
        func(help_seed_task, max_tries=3),
        func(help_sweep_task, max_tries=2),
        func(auto_backup_loss_task, max_tries=4),
        func(auto_backup_handoff_task, max_tries=4),
        func(auto_backup_open_task, max_tries=4),
        func(auto_backup_last_session_task, max_tries=4),
        func(backfill_cp_blobs_task, max_tries=2),
        # DOCX/PDF conversion is offloaded to the converter container; the task
        # itself is light (HTTP + DB write), so it stays on the default queue.
        # max_tries=2 because arq does not auto-retry plain exceptions — the
        # dead-letter branch fires on the first failure regardless (see
        # _convert_upload_task).
        func(convert_docx_task, max_tries=2),
        func(convert_pdf_task, max_tries=2),
        func(thin_auto_checkpoints_task, max_tries=2),
        func(telemetry_retention_task, max_tries=1),
        # The detached ComfyUI generation runs
        # here on the default queue — NOT as a web-process create_task (the web
        # process is reload-volatile; a create_task dies with an unreported
        # CancelledError on any .py edit/deploy/restart). max_tries=1: a retry re-
        # runs ComfyUI and persists a SECOND image + chip; duplicate output is worse
        # than a reported failure. Concurrency is bounded inside the task by the
        # COMFY_CONCURRENCY semaphore.
        func(generate_image_task, max_tries=1),
        # WHY: the irreversible inline-content null-out is guarded (see the
        # task docstring) and idempotent; it no-ops until backfill converges. Runs
        # on the default queue so a single cron lock (across replicas) bounds it.  Why: the null-out is irreversible, so it's guarded + idempotent (no-ops until backfill converges) and pinned to the default queue where the single cron lock bounds it to one run across replicas.
        func(nullout_inline_content_task, max_tries=1),
        # The pipeline-schedule tick (S3): claims + dispatches due
        # pipeline_schedules rows. max_tries=1 — the tick is idempotent (claim
        # semantics); a failed tick is re-run by the next minute's cron slot.
        func(pipeline_schedule_tick_task, max_tries=1),
        # WHY: widget_extract_task runs STT on the DEFAULT queue, deliberately
        # outside STT_CONCURRENCY: a chained design (transcribe_task →
        # transcription_complete → extraction) would fire the wet hook and
        # create a document — the one thing the widget extract surface must not
        # do — and a transcription-queue task would hold an STT slot for the LLM
        # half. max_tries=2 because a worker killed MID-task (a deploy during
        # the 2–3 min of STT+LLM) re-queues with job_try=2, and max_tries=1
        # would abort WITHOUT running the body — no recorder, the row sits in
        # 'transcribing' forever (the 2026-08-18 hole; precedent
        # convert_docx_task). Plain exceptions are terminal on try one
        # regardless (dead-letter on first failure); every status write is
        # idempotent, so the retry re-runs from the top.
        func(widget_extract_task, max_tries=2),
    ])

    # ARCH: GFS checkpoint thinning runs once daily off the web process. cron is a
    # singleton across worker replicas (arq holds a Redis lock per minute slot), so
    # scaling the default worker will not double-thin. max_tries=1 — thinning is
    # idempotent and cheap to re-run on the next day; a failed pass needs no retry storm.
    cron_jobs = [
        cron(thin_auto_checkpoints_task, hour=config.THIN_CRON_HOUR, minute=0, max_tries=1),
        # Telemetry retention runs once daily, offset 30min from thinning to avoid a
        # double DB-heavy pass in the same minute slot. cron is a singleton across
        # replicas (arq Redis lock), so scaling the worker won't double-purge.
        cron(telemetry_retention_task, hour=config.THIN_CRON_HOUR, minute=30, max_tries=1),
        # cp_blobs backfill + inline-content null-out converge the legacy dedup
        # system. Backfill runs hourly until no unmigrated rows remain (then a cheap
        # no-op); the null-out (guarded, irreversible) runs once daily offset from
        # the DB-heavy thinning pass. Both idempotent; singleton cron lock across
        # replicas prevents double-runs.
        cron(backfill_cp_blobs_task, minute=45, max_tries=2),
        # Embedding coverage sweep, hourly at :10 (offset from the :00/:15/:45
        # maintenance slots and the every-minute tick at :30s). Recovers docs
        # with zero chunks via the shared coverage predicate — the same
        # operation as POST /embed-missing, capped per run by EMBED_SWEEP_CAP.
        # cron is a singleton across replicas (arq Redis lock), so scaling the
        # worker will not double-sweep.
        cron(embed_sweep_task, minute=10, max_tries=1),
        cron(nullout_inline_content_task, hour=config.THIN_CRON_HOUR, minute=15, max_tries=1),
        # The ONE schedule tick — project-data
        # schedules are claimed + dispatched here, so a new scheduled pipeline is
        # a pipeline_schedules row, never a WorkerSettings edit. Every minute at
        # :30s (arq cron params are ints or omitted-for-every — a "*" string
        # crashes the cron loop at boot), offset from the minute-0/15/45 slots.
        cron(pipeline_schedule_tick_task, second=30, max_tries=1),
    ]


class TranscriptionWorkerSettings:
    redis_settings = _redis_settings()
    on_startup = _on_worker_startup
    on_shutdown = _on_worker_shutdown
    retry_jobs = True
    keep_result = 0
    max_jobs = config.STT_CONCURRENCY
    # INVARIANT: queue_name must equal the queue used at the enqueue sites  Why: arq routes by queue name; a mismatch (worker polls default 'arq:queue', jobs land in 'transcription') means they never meet — STT silently never runs.
    # (enqueue(..., queue="transcription")). Without it the worker polls arq's default
    # 'arq:queue' while transcribe jobs land in 'transcription' → transcription silently
    # never runs. Why: arq enqueue and worker consume must agree on the queue name.
    queue_name = TRANSCRIPTION_QUEUE

    # WHY: max_tries=2 bounds Retry-class deferrals and CancelledError re-runs
    # (worker shutdown cancels in-flight jobs) — NOT plain failures: under this
    # arq, retry_jobs covers only Retry/CancelledError, so a plain exception is
    # terminal on the first try and every transcribe failure path dead-letters
    # immediately (status='error' + transcription_error event; the contract's one
    # home is jobs.tasks.dead_letter). STT is slow/expensive — keep the bound low.
    functions = _guarded([func(transcribe_task, max_tries=2)])
