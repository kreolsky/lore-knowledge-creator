"""Lazy arq pool singleton and enqueue helper.

# ARCH: Single arq pool shared across the web process. Workers create their own
#       Redis connections via arq's built-in RedisSettings.
"""

import logging
from datetime import timedelta

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job

import config

logger = logging.getLogger(__name__)

_pool: ArqRedis | None = None

# WHY: arq silently drops jobs when the enqueue queue != worker queue_name.
# A single shared constant prevents the "enqueued but never consumed" failure.
# Both the enqueue sites and TranscriptionWorkerSettings.queue_name reference this.
TRANSCRIPTION_QUEUE: str = "transcription"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(config.REDIS_URL)


async def get_arq_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(_redis_settings())
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def enqueue(
    function_name: str,
    *args,
    job_id: str | None = None,
    defer: timedelta | int | None = None,
    queue: str | None = None,
    **kwargs,
) -> Job | None:
    """Post a job; returns arq's Job, or None when arq COLLAPSED it onto an
    already-pending job with the same job_id.

    # WHY: the return is the only signal separating a posting that will run
    # from one the stable-job_id dedup swallowed (the trailing-debounce
    # contract). Callers that bound work per run — the hourly embed sweep's
    # per-run cap — must count what LANDED; counting attempts spends the
    # budget on collapses that produce no provider load. Most callers post
    # fire-and-forget and ignore it.
    """
    pool = await get_arq_pool()
    defer_by = timedelta(seconds=defer) if isinstance(defer, (int, float)) else defer
    return await pool.enqueue_job(
        function_name,
        *args,
        _job_id=job_id,
        _defer_by=defer_by,
        _queue_name=queue,
        **kwargs,
    )
