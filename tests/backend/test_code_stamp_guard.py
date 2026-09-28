"""Unit tests for the stale-code canary guard (stale_code_guard).

The guard wraps a worker task coroutine and refuses the job when the worker's
CODE_STAMP diverges from the web process's fresh stamp (web:code_stamp in Redis),
running with grace when the web stamp is absent. arq 0.28 has no Worker.middleware
and raising in on_job_start crashes the worker (run_job exceptions re-raise via
t.result()), so the guard wraps the coroutine — a refusal is a normal arq job
failure, not a worker crash. These tests drive the guard directly with a fake
redis; the three branches (absent / equal / divergent) plus the redis-read failure
path are the canary's whole contract.
"""

from __future__ import annotations

import pytest
from code_stamp import CODE_STAMP, WEB_STAMP_REDIS_KEY, stale_code_guard


class _FakeRedis:
    """Stand-in for ctx['redis']: returns a fixed value (str|bytes|None) or raises."""

    def __init__(self, value):
        self._value = value
        self.requested_key: str | None = None

    async def get(self, key):
        self.requested_key = key
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _ctx(value):
    return {"redis": _FakeRedis(value), "job_id": "job-1"}


async def _task(ctx, *args, **kwargs):
    """The guarded task under test — records that it ran."""
    ctx["ran"] = True
    return "ran"


@pytest.mark.asyncio
async def test_absent_web_stamp_runs_with_grace():
    """Web stamp absent (web not booted / Redis flushed / startup race) → run."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx(None)
    assert await wrapped(ctx) == "ran"
    assert ctx["ran"] is True
    assert ctx["redis"].requested_key == WEB_STAMP_REDIS_KEY


@pytest.mark.asyncio
async def test_equal_web_stamp_runs():
    """Web stamp == worker CODE_STAMP → run (normal path)."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx(CODE_STAMP)  # str form
    assert await wrapped(ctx) == "ran"
    assert ctx["ran"] is True


@pytest.mark.asyncio
async def test_equal_web_stamp_runs_bytes_encoded():
    """The web publishes via the backplane (bytes); arq ctx['redis'].get returns
    bytes. A bytes-encoded equal stamp must still match (no false divergence)."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx(CODE_STAMP.encode())  # bytes form, as arq redis.get returns
    assert await wrapped(ctx) == "ran"
    assert ctx["ran"] is True


@pytest.mark.asyncio
async def test_divergent_web_stamp_refuses():
    """Web stamp present and != worker CODE_STAMP → refuse (raise), task never runs."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx(CODE_STAMP + "__stale")
    with pytest.raises(RuntimeError, match="stale worker code"):
        await wrapped(ctx)
    assert ctx.get("ran") is not True, "the guarded task must NOT run on divergence"


@pytest.mark.asyncio
async def test_divergent_bytes_web_stamp_refuses():
    """Divergence must also fire when the web stamp arrives bytes-encoded."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx((CODE_STAMP + "__stale").encode())
    with pytest.raises(RuntimeError):
        await wrapped(ctx)
    assert ctx.get("ran") is not True


@pytest.mark.asyncio
async def test_redis_read_failure_runs_best_effort():
    """A redis read failure must NEVER refuse (best-effort grace) — a transient
    redis blip must not masquerade as a stamp divergence and block every job."""
    wrapped = stale_code_guard(_task)
    ctx = _ctx(ConnectionError("redis down"))
    assert await wrapped(ctx) == "ran"
    assert ctx["ran"] is True
