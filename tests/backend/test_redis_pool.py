"""Contract tests for the ONE Redis owner (SYSTEM: redis-pool).

Plan `.kilo/plans/dependencies-point-down.md` step 1: every Redis client in
the tree is built by `redis_pool.make_redis`; the shared string client
(`get_redis`, decode_responses=True, bounded) replaces the per-module
singletons (rate_limit, agent/keys), and the backplane's
per-loop bytes connections come from the same factory with bounded=False.

These bind the factory's option contract, the singleton identity, and the
pubsub idle-timeout regression (a bounded subscriber would kill the backplane
listener — see backplane._ensure's WHY).
"""
import asyncio

import pytest
import redis_pool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError


@pytest.mark.asyncio
async def test_get_redis_returns_one_object_on_one_loop():
    """Two awaits on the same loop → one client (the shared string client)."""
    a = await redis_pool.get_redis()
    b = await redis_pool.get_redis()
    assert a is b


@pytest.mark.asyncio
async def test_close_redis_then_get_redis_yields_a_fresh_open_client():
    a = await redis_pool.get_redis()
    await redis_pool.close_redis()
    b = await redis_pool.get_redis()
    assert b is not a
    assert await b.ping() is True  # fresh AND open, not a closed leftover


@pytest.mark.asyncio
async def test_bounded_client_carries_the_moved_options_and_drops_timeout_retry():
    """The bounded options moved verbatim from rate_limit, with ONE change:
    retry_on_error loses RedisTimeoutError. Why: a ConnectionError means the
    command never reached the server (a stale socket — retry is safe); a
    TimeoutError may mean the server EXECUTED and the reply was lost, so
    retrying `SET key token NX` returns None for a key we already hold and
    completions answers 409 against our own token that nobody then releases
    before the TTL. The absence below IS the decision — assert it."""
    client = redis_pool.make_redis(decode_responses=True, bounded=True)
    try:
        kw = client.get_connection_kwargs()
        assert kw["decode_responses"] is True
        assert kw["socket_timeout"] == redis_pool._SOCKET_TIMEOUT_S
        assert kw["socket_connect_timeout"] == redis_pool._SOCKET_TIMEOUT_S
        assert kw["health_check_interval"] == redis_pool._HEALTH_CHECK_INTERVAL_S
        assert list(kw["retry_on_error"]) == [RedisConnectionError]
        assert RedisTimeoutError not in kw["retry_on_error"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unbounded_bytes_client_has_no_socket_timeout_and_returns_bytes():
    client = redis_pool.make_redis(decode_responses=False, bounded=False)
    try:
        # Absent and None are the same contract: no socket timeout applied (a
        # bare from_url omits the key entirely).
        assert client.get_connection_kwargs().get("socket_timeout") is None
        await client.set("rp:bytes:probe", "v")
        got = await client.get("rp:bytes:probe")
        assert got == b"v"  # bytes, not str — the backplane's contract
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unbounded_pubsub_survives_idle_and_still_receives():
    """The idle-timeout regression the plan names: redis-py applies
    socket_timeout to the BLOCKING pubsub read too (parse_response(block=True)
    → read_response(timeout=None) falls back to the socket timeout), so a
    bounded subscriber raises TimeoutError after _SOCKET_TIMEOUT_S of silence
    and the backplane listener dies into its 5s reconnect loop — dropping
    chat_frame and collab events every window. The pubsub client MUST be
    built bounded=False; this test goes red the day someone flips that call."""
    sub = redis_pool.make_redis(decode_responses=False, bounded=False)
    pub = await redis_pool.get_redis()
    channel = "rp:pubsub-idle"

    try:
        async with sub.pubsub() as ps:
            await ps.subscribe(channel)

            async def _next_message():
                # The backplane's read shape: a blocking listen() with NO
                # per-read timeout — the socket timeout is the only bound.
                async for message in ps.listen():
                    if message["type"] == "message":
                        return message

            reader = asyncio.create_task(_next_message())
            # Idle past 2× the bounded socket timeout with the reader parked
            # in the blocking read. A bounded client is dead by now.
            await asyncio.sleep(2 * redis_pool._SOCKET_TIMEOUT_S + 0.5)
            assert not reader.done(), (
                "pubsub reader died during idle — a bounded client's "
                "socket_timeout killed the blocking read"
            )
            await pub.publish(channel, "after-idle")
            message = await asyncio.wait_for(reader, timeout=5)
            assert message["data"] == b"after-idle"
    finally:
        await sub.aclose()
