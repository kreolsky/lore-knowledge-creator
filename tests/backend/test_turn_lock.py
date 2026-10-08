"""Stage 2 tests for the per-session turn lock (turn_lock.py, plan decision 11).

Validates the SET NX EX atomicity: a second acquire on the same chat_session is
rejected, and release frees it. The lock-busy REJECTION is create_completion's
job (409 JSON — see test_harness_turn.py); the fencing-token release (a slow
turn cannot erase a newer turn's lock) and the lease helpers that moved here
when the SSE pump died (heartbeat + shielded teardown, plan
agent-line-harness-lifecycle step 9) are covered below.

The compare-and-extend (heartbeat) is covered too: a heartbeat refreshes ONLY
THIS holder's lock — a stale heartbeat must never revive a lock already
re-acquired by a newer turn (the same fencing INVARIANT as the release).
"""
import time

import pytest

import config


class _FakeRedis:
    """Minimal in-memory redis: SET NX EX, GET, DEL, EVAL (compare-and-delete +
    compare-and-extend), with TTL tracking for the extend-refresh assertions."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.expires: dict[str, float] = {}  # key → monotonic expiry (0 = no ttl)
        self.extend_calls = 0

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.expires[key] = time.monotonic() + ex if ex else 0
        return True

    async def eval(self, script, numkeys, *args):
        key = args[0]
        if len(args) == 2:
            # Lua compare-and-delete: del only if the stored value == token.
            token = args[1]
            if self.store.get(key) == token:
                self.store.pop(key, None)
                self.expires.pop(key, None)
                return 1
            return 0
        # Lua compare-and-extend: refresh TTL only if the stored value == token.
        token, ttl = args[1], int(args[2])
        self.extend_calls += 1
        if self.store.get(key) == token:
            self.expires[key] = time.monotonic() + ttl
            return 1
        return 0

    async def delete(self, key):
        self.expires.pop(key, None)
        return self.store.pop(key, None) is not None


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get_redis():
        return fake

    # turn_lock resolves redis_pool.get_redis() at call time (the ONE seam).
    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_acquire_then_second_acquire_rejected(fake_redis):
    from turn_lock import acquire_turn_lock

    token = await acquire_turn_lock("s1")
    assert token is not None  # acquired → fencing token returned
    # A concurrent turn on the same chat_session is rejected (None).
    assert await acquire_turn_lock("s1") is None
    # Different session is independent.
    assert await acquire_turn_lock("s2") is not None


@pytest.mark.asyncio
async def test_release_allows_next_turn(fake_redis):
    from turn_lock import acquire_turn_lock, release_turn_lock

    token = await acquire_turn_lock("s1")
    await release_turn_lock("s1", token)
    # After release, a new turn can acquire.
    assert await acquire_turn_lock("s1") is not None


@pytest.mark.asyncio
async def test_empty_session_id_is_a_noop(fake_redis):
    from turn_lock import _NOOP_TOKEN, acquire_turn_lock, release_turn_lock

    # Legacy/Ask path carries no session id → no lock (the lock only governs the
    # harness path, which always carries a chat_session id).
    token = await acquire_turn_lock("")
    assert token == _NOOP_TOKEN
    await release_turn_lock("", token)  # no-op, no key written
    assert fake_redis.store == {}


@pytest.mark.asyncio
async def test_ttl_override_row_does_not_shadow_the_constant(
    fake_redis, test_db, monkeypatch,
):
    """An instance_settings row on TURN_LOCK_TTL_S does not reach the lock: the
    TTL is the config constant read at call time, so a
    stale override row left from the setting era must not stretch the lock the
    next acquire takes."""
    import json

    import settings
    from turn_lock import acquire_turn_lock

    monkeypatch.delenv("TURN_LOCK_TTL_S", raising=False)
    await test_db.query(
        "CREATE instance_settings CONTENT { key: 'TURN_LOCK_TTL_S', "
        "value: $v, updated_by: 'x', updated_at: time::now() }",
        {"v": json.dumps(300)},
    )
    settings.drop_cache()
    token = await acquire_turn_lock("s1")
    assert token is not None
    got = fake_redis.expires["pi:turn-lock:s1"] - time.monotonic()
    assert got <= config.TURN_LOCK_TTL_S + 1, (
        f"acquire took TTL {got:.0f}s — a TURN_LOCK_TTL_S row shadowed the "
        f"constant {config.TURN_LOCK_TTL_S}s"
    )


@pytest.mark.asyncio
async def test_fencing_release_does_not_erase_a_newer_turns_lock(fake_redis):
    """F1: a turn that outlives the TTL must NOT delete a newer turn's lock when it
    finally releases. acquire returns a unique token per holder; release is a
    compare-and-delete, so the stale token is a no-op."""
    from turn_lock import acquire_turn_lock, release_turn_lock

    # Turn A acquires, then (simulating TTL expiry) the key is gone + turn B
    # legitimately re-acquires under a fresh token.
    token_a = await acquire_turn_lock("s1")
    fake_redis.store.pop("pi:turn-lock:s1", None)  # simulate TTL expiry
    token_b = await acquire_turn_lock("s1")
    assert token_b is not None and token_b != token_a

    # Turn A finishes late and tries to release with its STALE token → must NOT
    # remove turn B's lock (compare-and-delete mismatch → no-op).
    await release_turn_lock("s1", token_a)
    assert fake_redis.store.get("pi:turn-lock:s1") == token_b, (
        "stale release must not erase the newer turn's lock"
    )
    # Turn B releases with its own token → succeeds.
    await release_turn_lock("s1", token_b)
    assert "pi:turn-lock:s1" not in fake_redis.store


# ─── compare-and-extend (heartbeat) — fencing preserved ──────────────────────
#
# Plan: chat-wedged-after-stop. The lock TTL was shortened (~30s) and a heartbeat
# refreshes it while the turn streams, so a leaked lock costs one heartbeat
# interval instead of 5.5 minutes. The extend is a compare-and-extend (NOT a bare
# EXPIRE): a stale heartbeat must never revive a lock already re-acquired by a
# newer turn — the same fencing INVARIANT as the release.


@pytest.mark.asyncio
async def test_extend_refreshes_ttl_for_the_holder(fake_redis):
    from turn_lock import acquire_turn_lock, extend_turn_lock

    token = await acquire_turn_lock("s1", ttl=5)
    before = fake_redis.expires["pi:turn-lock:s1"]
    # A heartbeat extends THIS holder's lock.
    ok = await extend_turn_lock("s1", token, ttl=30)
    assert ok
    after = fake_redis.expires["pi:turn-lock:s1"]
    assert after > before, "TTL must be refreshed (reset to now + ttl)"
    # The holder's fencing value is unchanged.
    assert fake_redis.store["pi:turn-lock:s1"] == token


@pytest.mark.asyncio
async def test_extend_is_a_noop_for_a_mismatched_token(fake_redis):
    """Fencing: a heartbeat MUST NOT refresh a lock already held by another turn.

    Mirrors the release fencing test: if a turn outlives the TTL, its lock expires
    and a second turn legitimately re-acquires it under a fresh token. The first
    turn's (still-running) heartbeat must NOT revive the second turn's lock."""
    from turn_lock import acquire_turn_lock, extend_turn_lock

    token_a = await acquire_turn_lock("s1")
    # Simulate TTL expiry + re-acquire by turn B under a fresh token.
    fake_redis.store.pop("pi:turn-lock:s1", None)
    token_b = await acquire_turn_lock("s1")
    assert token_b != token_a

    # Turn A's stale heartbeat fires → must NOT refresh turn B's lock.
    ok = await extend_turn_lock("s1", token_a, ttl=30)
    assert not ok
    assert fake_redis.store.get("pi:turn-lock:s1") == token_b, (
        "stale heartbeat must not touch the newer turn's lock"
    )


@pytest.mark.asyncio
async def test_extend_noop_for_empty_session(fake_redis):
    from turn_lock import _NOOP_TOKEN, extend_turn_lock

    # Legacy/Ask path carries no session id → no lock to extend (no-op).
    await extend_turn_lock("", _NOOP_TOKEN, ttl=30)
    assert fake_redis.store == {}
    assert fake_redis.extend_calls == 0


# ─── the lease helpers (moved from turn_lock_lease, step 9) ───────────────────


@pytest.mark.asyncio
async def test_heartbeat_extends_until_the_turn_ends(fake_redis, monkeypatch):
    """The lease heartbeat refreshes the holder's TTL on a cadence; the
    teardown (the channel on_end's release seam) stops it and releases."""
    import asyncio

    from turn_lock import (
        _heartbeat_turn_lock,
        _teardown_turn_lock,
        acquire_turn_lock,
    )

    monkeypatch.setattr(config, "TURN_LOCK_HEARTBEAT_S", 0.01)
    token = await acquire_turn_lock("s1", ttl=5)
    before = fake_redis.expires["pi:turn-lock:s1"]

    hb = asyncio.ensure_future(_heartbeat_turn_lock("s1", token))
    try:
        await asyncio.sleep(0.06)
        # Sample BEFORE teardown — the release deletes the key (and its TTL
        # tracking) from the fake, so the refresh must be witnessed live.
        mid = fake_redis.expires["pi:turn-lock:s1"]
    finally:
        await _teardown_turn_lock("s1", token, hb)

    assert mid > before, (
        "at least one beat must have refreshed the TTL"
    )
    assert "pi:turn-lock:s1" not in fake_redis.store, (
        "teardown must release the lock"
    )


@pytest.mark.asyncio
async def test_teardown_without_token_is_a_noop(fake_redis):
    from turn_lock import _teardown_turn_lock

    await _teardown_turn_lock("s1", None, None)
    assert fake_redis.store == {}


# ─── wedged-socket character (redis_pool's bounded client) ───────────────────


@pytest.mark.asyncio
async def test_socket_timeout_on_acquire_raises_not_returns_none(monkeypatch):
    """A WEDGED socket (not a busy lock) raises OUT of acquire — never returns
    None, which create_completion's 409 path would read as 'turn in progress'
    against a turn nobody holds. redis_pool's bounded client raises
    redis.exceptions.TimeoutError within _SOCKET_TIMEOUT_S instead of hanging
    forever on the old unbounded connection."""
    import redis.exceptions as redis_exc

    class _WedgedRedis:
        async def set(self, *_a, **_k):
            raise redis_exc.TimeoutError("socket timeout")

    async def _get_redis():
        return _WedgedRedis()

    import redis_pool

    monkeypatch.setattr(redis_pool, "get_redis", _get_redis)
    from turn_lock import acquire_turn_lock

    with pytest.raises(redis_exc.TimeoutError):
        await acquire_turn_lock("s1")


# ─── turn_lock_held: the read side of the lock (real Redis) ──────────────────


@pytest.mark.asyncio
async def test_turn_lock_held_follows_acquire_and_release():
    """Against the real per-worker Redis: held after acquire, free after the
    holder's release — the messages read marks the open turn on this answer."""
    from turn_lock import acquire_turn_lock, release_turn_lock, turn_lock_held

    assert await turn_lock_held("s-held") is False
    token = await acquire_turn_lock("s-held")
    assert token
    assert await turn_lock_held("s-held") is True
    assert await turn_lock_held("s-other") is False
    await release_turn_lock("s-held", token)
    assert await turn_lock_held("s-held") is False
