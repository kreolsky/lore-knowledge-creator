"""run_id resolution for the memory consolidation loop tools.

The loop tools (`next_reference` / `apply_memory_verdicts`) must NOT require a
hand-copied 36-char run_id: one transcription slip silently lost a whole batch (the
observed 404). With no run_id but the session that started the run, they resolve the
session's active run. With no run and no session, they 400 LOUDLY — never a silently
wrong run.

These unit tests cover the resolver + the per-session Redis pointer in isolation
(`resolve_run_id` / `record_active_run` / `resolve_active_run`); the route-level
contract is covered by `test_memory_consolidate.py`.
"""
import pytest
from fastapi import HTTPException


class _FakeRedis:
    """Minimal in-memory redis: set (ignores ex) + get. The real client uses
    decode_responses=True, so values are stored/returned as-is (str)."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    async def get(self, key: str) -> str | None:
        return self.store.get(key)


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    # The resolver resolves redis_pool.get_redis() at call time (the ONE seam),
    # so patching the attribute here is picked up on each call.
    monkeypatch.setattr("redis_pool.get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_record_and_resolve_active_run_roundtrip(fake_redis):
    from memory.run_key import record_active_run, resolve_active_run

    await record_active_run("sess-1", "run-abc")
    assert await resolve_active_run("sess-1") == "run-abc"
    assert await resolve_active_run("no-such-session") is None


@pytest.mark.asyncio
async def test_record_active_run_is_a_noop_without_a_session(fake_redis):
    from memory.run_key import record_active_run, resolve_active_run

    # No session_id (MCP / no-session surface) → nothing recorded, no raise.
    await record_active_run(None, "run-x")
    assert fake_redis.store == {}
    assert await resolve_active_run(None) is None


@pytest.mark.asyncio
async def test_resolve_run_id_explicit_value_wins_over_session(fake_redis):
    from memory.run_key import record_active_run, resolve_run_id

    await record_active_run("sess-1", "run-session")
    # An explicit run_id is used UNCHANGED — the session pointer never overrides a
    # caller that knows which run it means.
    assert await resolve_run_id(run_id="run-explicit", session_id="sess-1") == "run-explicit"


@pytest.mark.asyncio
async def test_resolve_run_id_uses_the_sessions_active_run_when_omitted(fake_redis):
    from memory.run_key import record_active_run, resolve_run_id

    await record_active_run("sess-1", "run-abc")
    assert await resolve_run_id(run_id=None, session_id="sess-1") == "run-abc"
    # An empty string is treated like an omission (the model sometimes emits "").
    assert await resolve_run_id(run_id="", session_id="sess-1") == "run-abc"


@pytest.mark.asyncio
async def test_resolve_run_id_raises_400_when_no_run_and_no_session(fake_redis):
    from memory.run_key import resolve_run_id

    with pytest.raises(HTTPException) as ei:
        await resolve_run_id(run_id=None, session_id=None)
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_run_id_raises_400_when_session_has_no_active_run(fake_redis):
    from memory.run_key import resolve_run_id

    # A session that never started (or whose pointer expired) → loud 400, never a
    # silently wrong run.
    with pytest.raises(HTTPException) as ei:
        await resolve_run_id(run_id=None, session_id="sess-stale")
    assert ei.value.status_code == 400
