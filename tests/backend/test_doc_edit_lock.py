"""Tests for the Redis per-key edit lock (SYSTEM: doc-edit-lock).

The lock replaced a process-local asyncio.Lock registry whose own docstring
named the gap ("CROSS-REPLICA LIMITATION"): two REST writes to one document on
different replicas both reached the convergence write and raced into a lost
update while both callers received {"status":"applied"} — reproduced live on
the two-replica gray stack (5/10 concurrent rounds lost an edit, all 200s).
These tests pin the Redis lock's contract: SET NX EX acquire, fencing-token
compare-and-delete release, TTL-unblocks-dead-holder, fail-loud on Redis
errors, and the write-under-held-lock ordering invariant.
"""

import asyncio
import time

import pytest


class _FakeRedis:
    """Minimal async redis: SET NX EX + TTL expiry + EVAL compare-and-delete."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expires: dict[str, float | None] = {}

    def _expired(self, key) -> bool:
        dl = self.expires.get(key)
        return dl is not None and dl <= time.monotonic()

    async def set(self, key, value, *, nx=False, ex=None):
        if key in self.store and not self._expired(key) and nx:
            return None
        self.store[key] = value
        self.expires[key] = time.monotonic() + ex if ex else None
        return True

    async def eval(self, script, numkeys, *args):
        # Lua compare-and-delete: del only if the stored value == this token.
        key, token = args[0], args[1]
        if key in self.store and not self._expired(key) and self.store[key] == token:
            del self.store[key]
            self.expires.pop(key, None)
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)
    import doc_edit_lock

    monkeypatch.setattr(doc_edit_lock, "_ACQUIRE_POLL_S", 0)
    return fake


def _held(fake, key):
    from doc_edit_lock import _LOCK_KEY_PREFIX

    return _LOCK_KEY_PREFIX + key in fake.store


# ─── acquisition, mutual exclusion, release ───────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_holds_key_until_release(fake_redis):
    from doc_edit_lock import edit_lock

    async with edit_lock("d1"):
        assert _held(fake_redis, "d1")
    assert not _held(fake_redis, "d1")


@pytest.mark.asyncio
async def test_contention_loser_waits_then_proceeds(fake_redis):
    """The loser BLOCKS until the holder releases (same shape as the deleted
    asyncio.Lock), then re-resolves through the caller's drift handling — the
    body runs strictly after the holder's body."""
    from doc_edit_lock import edit_lock

    order = []

    async def winner():
        async with edit_lock("d1"):
            order.append("winner-body")
            await asyncio.sleep(0.05)
        order.append("winner-release")

    async def loser():
        async with edit_lock("d1"):
            order.append("loser-body")

    await asyncio.gather(winner(), loser())
    assert order == ["winner-body", "winner-release", "loser-body"]


@pytest.mark.asyncio
async def test_different_keys_do_not_block_each_other(fake_redis):
    """Distinct keys = distinct locks (guards against an accidental global lock;
    the registry property the asyncio version had, now per Redis key)."""
    from doc_edit_lock import edit_lock

    async with edit_lock("d1"):
        async with edit_lock("d2"):
            assert _held(fake_redis, "d1") and _held(fake_redis, "d2")


@pytest.mark.asyncio
async def test_two_waiters_fifo_safety_after_release(fake_redis):
    """Both waiters eventually acquire; no acquire is starved past release."""
    from doc_edit_lock import edit_lock

    done = []

    async def worker(n):
        async with edit_lock("d1"):
            done.append(n)

    async with edit_lock("d1"):
        tasks = [asyncio.create_task(worker(n)) for n in range(3)]
        await asyncio.sleep(0.02)
        assert done == []
    await asyncio.gather(*tasks)
    assert sorted(done) == [0, 1, 2]


# ─── fencing + TTL ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_is_fenced_against_reacquired_lock(fake_redis):
    """A holder that outlived its TTL must not delete the NEXT holder's lock
    (compare-and-delete, not bare DEL — same INVARIANT as turn_lock)."""
    from doc_edit_lock import edit_lock

    lock_a = edit_lock("d1", ttl=0.05)
    await lock_a.__aenter__()
    token_a = fake_redis.store["editlock:d1"]
    await asyncio.sleep(0.08)  # A's TTL lapses; key now expired
    async with edit_lock("d1") as _:
        token_b = fake_redis.store["editlock:d1"]
        assert token_b != token_a
        await lock_a.__aexit__(None, None, None)  # A's stale release
        assert fake_redis.store["editlock:d1"] == token_b  # B still holds


@pytest.mark.asyncio
async def test_ttl_expiry_unblocks_dead_holder(fake_redis):
    """A replica dying mid-apply must not wedge the document: the TTL lapses and
    the waiter proceeds within one poll interval of expiry."""
    from doc_edit_lock import edit_lock

    async with edit_lock("d1", ttl=0.05):
        started = asyncio.create_task(_acquire_and_flag("d1", ttl=60))
        await asyncio.sleep(0.02)
        assert not started.done()  # blocked while the (dead) holder's key exists
        await asyncio.sleep(0.1)  # TTL lapses
        await asyncio.wait_for(started, timeout=1)


async def _acquire_and_flag(key: str, *, ttl: int) -> None:
    from doc_edit_lock import edit_lock

    async with edit_lock(key, ttl=ttl):
        pass


# ─── failure character ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_error_during_acquire_raises(fake_redis, monkeypatch):
    """Fail LOUD: with Redis unreachable the apply must not proceed unlocked —
    silent data loss behind a success response is the defect this module exists
    to close, and running the body without the lock would re-open it."""

    async def _broken():
        raise ConnectionError("Redis down")

    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _broken)
    from doc_edit_lock import edit_lock

    with pytest.raises(ConnectionError):
        async with edit_lock("d1"):
            pass


@pytest.mark.asyncio
async def test_release_failure_is_best_effort(fake_redis, monkeypatch):
    """A failed RELEASE must not crash the caller's tail (the edit already
    committed); the TTL reaps the orphaned key."""

    original_eval = fake_redis.eval

    async def _flaky_eval(script, numkeys, *args):
        raise ConnectionError("Redis down")

    from doc_edit_lock import edit_lock

    async with edit_lock("d1"):
        fake_redis.eval = _flaky_eval
    fake_redis.eval = original_eval
    # Key remains held (release failed) — TTL reaps it; nothing raised.


# ─── the lock-ordering invariant, observed at the write path ─────────────────


@pytest.mark.asyncio
async def test_convergence_write_runs_under_held_lock(fake_redis, monkeypatch):
    """INVARIANT(corruption) observed live: by the time the convergence write
    (route_document_edits — the thing that nests session._write_lock INSIDE)
    runs, the per-doc Redis lock for that document is HELD. doc_edit_lock is
    acquired first, session._write_lock second — never the reverse."""
    from agent import collab_writes as cw
    from agent import tool_api_surface as tus

    live_states = {"d1": "alpha tail"}
    observed = {}

    async def fake_fetch_one(table, rid):
        if table == "documents":
            return {"project_id": "p-1", "content": live_states[rid]}
        return None

    async def fake_access(_did, _user):
        return "full"

    async def fake_resolve(doc_id):
        return live_states[doc_id], "{}"

    async def fake_checkpoint(**kw):
        return {"checkpoint_id": "cp"}

    async def fake_route_edits(*, doc_id, edits, project_id):
        observed["held_at_write"] = _held(fake_redis, doc_id)
        live_states[doc_id] = live_states[doc_id].replace(
            edits[0]["new_text"], edits[0]["new_text"]
        )
        return True

    async def fake_presence(*_a, **_kw):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr("agent.doc_state.route_document_edits", fake_route_edits)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    result = await tus.apply_edit_to_document(
        doc_id="d1", old_string="alpha", new_text="ALPHA",
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert observed["held_at_write"] is True
    assert not _held(fake_redis, "d1")  # released after the write


# ─── acquire ceiling — an unbounded poll would run the body UNPROTECTED ───────


@pytest.mark.asyncio
async def test_acquire_raises_at_ceiling_instead_of_polling_forever(fake_redis, monkeypatch):
    """A wait past the ceiling raises EditLockTimeout rather than waiting the TTL
    out and then proceeding. Why this matters: without the ceiling a re-entry (or
    a starved waiter) eventually acquires a key the holder still believes it owns,
    and the body runs unserialized — the lost-update this module exists to close,
    on a timer instead of an instant deadlock."""
    import doc_edit_lock
    from doc_edit_lock import EditLockTimeout, edit_lock

    monkeypatch.setattr(doc_edit_lock, "DOC_EDIT_LOCK_ACQUIRE_CEILING_S", 0.05)
    async with edit_lock("d-ceiling", ttl=60):
        with pytest.raises(EditLockTimeout):
            async with edit_lock("d-ceiling", ttl=60):
                pass


@pytest.mark.asyncio
async def test_ceiling_timeout_is_a_timeouterror(fake_redis, monkeypatch):
    """EditLockTimeout subclasses TimeoutError so callers already mapping timeouts
    keep working."""
    import doc_edit_lock
    from doc_edit_lock import edit_lock

    monkeypatch.setattr(doc_edit_lock, "DOC_EDIT_LOCK_ACQUIRE_CEILING_S", 0.05)
    async with edit_lock("d-ceiling-2", ttl=60):
        with pytest.raises(TimeoutError):
            async with edit_lock("d-ceiling-2", ttl=60):
                pass


@pytest.mark.asyncio
async def test_ceiling_does_not_fire_on_ordinary_contention(fake_redis):
    """The ceiling is a bug detector, not a load limit: a normal contention loser
    (holder releases well inside the ceiling) still acquires and proceeds."""
    from doc_edit_lock import edit_lock

    order = []

    async def waiter():
        async with edit_lock("d-normal"):
            order.append("waiter")

    async with edit_lock("d-normal"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        order.append("holder")
    await task
    assert order == ["holder", "waiter"]


@pytest.mark.asyncio
async def test_socket_timeout_during_acquire_raises_out(monkeypatch):
    """A wedged socket raises out of edit_lock.__aenter__ — the acquire never
    proceeds unlocked (the data-loss INVARIANT) and no longer hangs forever:
    redis_pool's bounded client raises redis.exceptions.TimeoutError within
    _SOCKET_TIMEOUT_S, riding the documented raise into the Tool-API's error
    envelope (the same path test_redis_error_during_acquire_raises pins for
    ConnectionError)."""

    import redis.exceptions as redis_exc

    class _WedgedRedis:
        async def set(self, *_a, **_k):
            raise redis_exc.TimeoutError("socket timeout")

    async def _get_redis():
        return _WedgedRedis()

    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)
    from doc_edit_lock import edit_lock

    with pytest.raises(redis_exc.TimeoutError):
        async with edit_lock("d1"):
            pass
