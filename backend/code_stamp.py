"""Worker code-version stamp — detect a worker running stale jobs/pipeline code.

# ARCH: The web process and the arq worker run from separate container images rebuilt
# independently. `docker compose up --build backend` does NOT rebuild the worker (a
# documented foot-gun: backend/jobs/** and backend/pipeline/** changes silently keep
# running old code). This stamp converts that into a monitorable signal: every process
# hashes the same source trees at import; the worker publishes its stamp to Redis on
# boot, and the web process compares its own stamp against it on /api/health
# (worker_stale: true when they diverge).
#
# Fail-loud canary: the web ALSO publishes its fresh stamp (web:code_stamp) and the
# worker's stale_code_guard refuses any job whose CODE_STAMP diverges from it — turning
# the passive /api/health signal into an active refusal so a stale worker cannot silently
# emit wrong output. Grace when the web stamp is absent (web not booted / Redis flushed).
"""

import functools
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Trees whose code runs in the worker process, not the web process.
_STAMPED_TREES = ("jobs", "pipeline")
WORKER_STAMP_REDIS_KEY = "worker:code_stamp"
WEB_STAMP_REDIS_KEY = "web:code_stamp"
# TTL on the web's published stamp + refresh cadence. Why: Redis is persistent
# (--appendonly + a volume), so a no-TTL stamp left by a PRIOR deploy — or a dead web
# whose best-effort publish failed — is indistinguishable from genuine divergence and
# would falsely refuse every job on a fresh worker that boots during the redeploy
# window. The TTL makes a stale reference expire so the worker degrades to the
# absent-grace branch; the web refreshes it (well within the TTL) while alive so the
# canary stays continuously active. See _web_stamp_heartbeat in main.py.
WEB_STAMP_TTL_S = 600.0       # 10 min — survives a brief web blip/restart
WEB_STAMP_REFRESH_S = 180.0   # 3 min — refreshed well within the TTL


def compute_code_stamp() -> str:
    """Short stable hash of every .py under the worker-owned source trees."""
    base = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for tree in _STAMPED_TREES:
        root = base / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            h.update(path.relative_to(base).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:12]


CODE_STAMP = compute_code_stamp()


def stale_code_guard(coro):
    """Refuse a worker job when CODE_STAMP diverges from the web's fresh stamp.

    Wraps an arq task COROUTINE (`async def(ctx, *args, **kwargs)`). It runs as the
    first thing inside the job's execution, so a refusal raises inside arq's job
    try/except → a normal job failure (retry-then-fail), NOT a worker crash.

    # ARCH: arq 0.28 has NO Worker.middleware, and raising in on_job_start propagates
    #       through run_job → main()'s `t.result()` and CRASHES the whole worker. The
    #       coroutine-wrapper is the clean equivalent: the guard executes in the job's
    #       own exception context. Applied to every task in both WorkerSettings +
    #       TranscriptionWorkerSettings.functions (cron jobs are unguarded — idempotent
    #       maintenance).
    # INVARIANT: a missing web reference never refuses. Why: web boots after worker,
    #       Redis flushes, and startup races all make the web stamp transiently absent —
    #       refusing then would block ALL jobs on a healthy worker for a non-divergence.
    #       Only a PRESENT-and-non-equal value is positive evidence of divergence.
    """
    @functools.wraps(coro)
    async def wrapper(ctx, *args, **kwargs):
        try:
            raw = await ctx["redis"].get(WEB_STAMP_REDIS_KEY)
        except Exception:
            # Best-effort grace: a transient redis blip must not masquerade as a
            # stamp divergence and block every job.
            logger.warning(
                "stale-code guard: could not read web stamp (job %s) — running best-effort",
                ctx.get("job_id", "?"), exc_info=True,
            )
            return await coro(ctx, *args, **kwargs)
        web_stamp = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if web_stamp is not None and web_stamp != CODE_STAMP:
            logger.critical(
                "stale-code canary: worker code_stamp %s != web %s — refusing job %s; "
                "rebuild the worker image (docker compose build worker worker-transcription)",
                CODE_STAMP, web_stamp, ctx.get("job_id", "?"),
            )
            raise RuntimeError(
                f"stale worker code: worker stamp {CODE_STAMP} != web stamp {web_stamp} "
                f"— rebuild the worker image"
            )
        if web_stamp is None:
            logger.warning(
                "stale-code guard: web code_stamp absent (job %s) — running without a freshness reference",
                ctx.get("job_id", "?"),
            )
        return await coro(ctx, *args, **kwargs)
    return wrapper
