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

import pytest
import redis_pool


@pytest.mark.asyncio
async def test_close_redis_then_get_redis_yields_a_fresh_open_client():
    a = await redis_pool.get_redis()
    await redis_pool.close_redis()
    b = await redis_pool.get_redis()
    assert b is not a
    assert await b.ping() is True  # fresh AND open, not a closed leftover
