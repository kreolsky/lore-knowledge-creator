"""The ONE Redis owner — every client in the tree comes from here.

# SYSTEM: redis-pool — the single Redis client factory + shared string client
#
# ARCH: dependencies-point-down (audit F2/N5): three modules used to build
# their own redis.asyncio clients — rate_limit (bounded), agent/keys (bare
# from_url, decode_responses=True), backplane
# (bytes, per-event-loop) — with drifting options and test seams.
# make_redis is the ONE factory (it carries REDIS_URL); get_redis() is the
# process-wide shared string client; the backplane's per-loop bytes
# connections come from the same factory with bounded=False. arq's own
# RedisSettings (jobs/pool.py) stays arq's.
#
# INVARIANT: the client is redis.asyncio and every check is awaited — never a
# sync client called from an async handler. Why: check_api_key_rate_limit runs
# on EVERY api-key request (the tool-api path dsh drives) and the mcp transport
# tier runs at the ASGI gate; a sync round-trip there blocks the whole event
# loop, so one WEDGED (not refused) Redis connection stalls every concurrent
# request on the replica for socket_timeout, not just the caller. Bounded I/O
# off the loop is the same reason /api/health wraps its probes in wait_for.
"""

import config

# One reconnect attempt before a caller decides "Redis is down". Why: a pooled
# connection goes stale across a Redis restart or an idle reap, and the first
# command on it raises ConnectionError. Without a retry that reconnect is
# indistinguishable from an outage and fails closed — a legitimate login would
# eat a spurious 429. Fail-closed must mean "Redis is down", not "the socket
# was old". health_check_interval PINGs an idle connection for the same reason.
_RETRY_ATTEMPTS = 1
_HEALTH_CHECK_INTERVAL_S = 30
_SOCKET_TIMEOUT_S = 2.0

# The ONE option change on the move from rate_limit: retry_on_error DROPS
# RedisTimeoutError (rate_limit retried both). Why: a ConnectionError means the
# command never reached the server (the stale-socket case the retry rationale
# above names), so a retry is safe; a TimeoutError may mean the server EXECUTED
# and the reply was lost, and retrying `SET key token NX` (turn_lock acquire)
# then returns None for a key we already hold — completions would answer 409
# "turn in progress" against our own token and nobody releases it before the
# TTL. Rate-limit tiers lose the retry on a timed-out command only; they still
# fail closed.

_redis = None


def make_redis(*, decode_responses: bool, bounded: bool):
    """The ONE factory every Redis client comes from (carries REDIS_URL).

    decode_responses picks the string (True) or bytes (False) protocol — the
    backplane is the bytes consumer; everything else shares the string client.
    bounded=True adds the socket/health/retry options above; bounded=False
    emits a bare from_url. WHY bounded=False exists at all: redis-py applies
    socket_timeout to the BLOCKING pubsub read too (parse_response(block=True)
    → read_response(timeout=None) falls back to socket_timeout), so a bounded
    subscriber dies after _SOCKET_TIMEOUT_S of idle — pubsub consumers MUST
    pass bounded=False (the WHY at backplane._ensure's call pins this).
    """
    import redis.asyncio as aioredis

    if not bounded:
        return aioredis.from_url(config.REDIS_URL, decode_responses=decode_responses)

    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError
    from redis.retry import Retry

    return aioredis.from_url(
        config.REDIS_URL,
        decode_responses=decode_responses,
        socket_timeout=_SOCKET_TIMEOUT_S,
        socket_connect_timeout=_SOCKET_TIMEOUT_S,
        health_check_interval=_HEALTH_CHECK_INTERVAL_S,
        retry=Retry(ExponentialBackoff(cap=0.2, base=0.02), _RETRY_ATTEMPTS),
        retry_on_error=[RedisConnectionError],  # TimeoutError dropped — see above
    )


async def get_redis():
    """The process-wide shared string client (decode_responses=True, bounded).

    Consumers reach it as `redis_pool.get_redis()` — module-attribute lookup
    at call time — so ONE test seam (patching redis_pool.get_redis) reaches
    every consumer; a top-level `from redis_pool import get_redis` in a
    consumer would freeze the binding at import and make that patch succeed
    while inert.
    """
    global _redis
    if _redis is None:
        _redis = make_redis(decode_responses=True, bounded=True)
    return _redis


async def close_redis():
    """Close the shared client (process shutdown); the next get_redis() builds
    a fresh one. The slot is cleared BEFORE the close so a caller racing the
    shutdown path builds a new client instead of receiving the dying one."""
    global _redis
    client, _redis = _redis, None
    if client is not None:
        await client.aclose()
