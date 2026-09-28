"""Backplane — Redis pub/sub for inter-replica Y.Doc sync and event_bus fan-out.

# SYSTEM: backplane — inter-replica pub/sub for CRDT Y.Doc sync
# ARCH: Backplane fans out Yjs updates across backend replicas so any client
#       connected to any replica receives edits. Also carries event_bus events
#       (access_changed, entity_deleted, and the rest) cross-process via the evt: namespace.
# INVARIANT: Redis mandatory — Redis down = service down. No in-process fallback.  Why: the backplane is the only cross-process signal channel; an in-process fallback would silently drop broadcasts, so a Redis outage fails hard rather than degrade.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import redis_pool
import settings

logger = logging.getLogger(__name__)

YDOC_CHANNEL_PREFIX = "ydoc:"
EVT_CHANNEL_PREFIX = "evt:"


class Backplane(ABC):
    @abstractmethod
    async def publish(self, channel: str, data: bytes) -> None:
        ...

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        ...

    @abstractmethod
    async def subscribe(self, channel: str, handler: Callable[[bytes], None]) -> None:
        ...

    @abstractmethod
    async def unsubscribe(self, channel: str) -> None:
        ...

    @abstractmethod
    async def sadd(self, key: str, member: str) -> None:
        ...

    @abstractmethod
    async def smembers(self, key: str) -> set[bytes]:
        ...

    @abstractmethod
    async def sismember(self, key: str, member: str) -> bool:
        ...

    @abstractmethod
    async def sadd_ttl(self, key: str, member: str, ttl_s: float) -> None:
        """SADD + EXPIRE in ONE transaction — a set created here can never exist
        without a TTL (a crash between separate SADD and EXPIRE calls would
        strand a TTL-less key)."""

    @abstractmethod
    async def srem(self, key: str, member: str) -> None:
        ...

    @abstractmethod
    async def expire(self, key: str, ttl_s: float) -> None:
        ...


class _LoopConn:
    """Per-event-loop Redis connection set + handler routing table.

    redis.asyncio connection objects bind to the event loop that first awaits
    them; awaiting them from another loop deadlocks. We therefore key one
    connection set per running loop.
    """

    def __init__(self) -> None:
        self.pub: Any = None
        self.sub: Any = None
        # WHY: list of handlers per channel — a channel may have multiple
        # subscribers in one process (e.g. a test simulating fan-out). Why: the
        # old single-handler dict silently dropped all but the last subscriber.
        self.handlers: dict[str, list[Callable[[bytes], None]]] = {}
        self.listener_task: asyncio.Task | None = None


class RedisBackplane(Backplane):
    """Redis pub/sub backplane for multi-process deployments.

    # WHY: connections are kept per running event loop. Why: redis.asyncio
    # objects have loop affinity; production runs one uvicorn loop, but the test
    # harness drives both a pytest-asyncio loop (httpx) and a Starlette TestClient
    # portal loop against this process-global singleton. Sharing one connection
    # set across both loops deadlocks on the first cross-loop await (see the WS
    # collab tests). Delivery across loops still works because Redis is the broker.
    """

    def __init__(self) -> None:
        self._conns: dict[asyncio.AbstractEventLoop, _LoopConn] = {}

    def _conn(self) -> _LoopConn:
        loop = asyncio.get_running_loop()
        conn = self._conns.get(loop)
        if conn is None:
            conn = _LoopConn()
            self._conns[loop] = conn
        return conn

    async def _ensure(self, conn: _LoopConn) -> None:
        if conn.pub is not None:
            return
        # WHY bounded=False is MANDATORY here: redis-py applies socket_timeout
        # to the BLOCKING pubsub read too (PubSub.parse_response(block=True) →
        # Connection.read_response(timeout=None) falls back to socket_timeout),
        # so a bounded subscriber raises TimeoutError after _SOCKET_TIMEOUT_S
        # of idle, listen() below exits into the "listener crashed" reconnect
        # branch, and every 5s reconnect window drops chat_frame and collab
        # events. The per-loop _LoopConn shape stays (see the WHY on the class).
        conn.pub = redis_pool.make_redis(decode_responses=False, bounded=False)
        conn.sub = conn.pub.pubsub()

    async def publish(self, channel: str, data: bytes) -> None:
        conn = self._conn()
        # WHY: bounded by BACKPLANE_PUBLISH_TIMEOUT_S. Why: pub/sub publish is
        # fire-and-forget — every caller already tolerates a publish failure via
        # try/except. An unbounded await hangs the whole process on a wedged Redis
        # connection (DinD CI: 120s suite kill, no traceback). The timeout converts
        # that hang into the already-handled asyncio.TimeoutError path.
        publish_timeout_s = await settings.get("BACKPLANE_PUBLISH_TIMEOUT_S")
        await asyncio.wait_for(self._ensure(conn), timeout=publish_timeout_s)
        await asyncio.wait_for(conn.pub.publish(channel, data), timeout=publish_timeout_s)

    async def get(self, key: str) -> bytes | None:
        conn = self._conn()
        await self._ensure(conn)
        return await conn.pub.get(key)

    async def set(self, key: str, value: str) -> None:
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.set(key, value)

    async def set_nx(self, key: str, value: str, *, ttl_s: float) -> bool:
        """SET if Not eXists, with a TTL (ms). Returns True if this call SET the key
        (won), False if it already existed. The
        upload redeem claims a jti slot here so a replay of the SAME token returns the
        already-created node id instead of creating a second node."""
        conn = self._conn()
        await self._ensure(conn)
        return bool(await conn.pub.set(key, value, nx=True, px=int(ttl_s * 1000)))

    async def set_ttl(self, key: str, value: str, *, ttl_s: float) -> None:
        """SET with a TTL (ms), overwriting any existing value (unconditional). Pairs
        with set_nx: a claim is backfilled with the real value while keeping the TTL."""
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.set(key, value, px=int(ttl_s * 1000))

    async def delete(self, key: str) -> None:
        """DEL. Pairs with set_nx: release a claim whose owner failed BEFORE backfill,
        so the same token is not locked out for the whole TTL (a transient failure must
        not turn a retry into a 409 until the key expires)."""
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.delete(key)

    async def sadd(self, key: str, member: str) -> None:
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.sadd(key, member)

    async def smembers(self, key: str) -> set[bytes]:
        conn = self._conn()
        await self._ensure(conn)
        return set(await conn.pub.smembers(key))

    async def sismember(self, key: str, member: str) -> bool:
        conn = self._conn()
        await self._ensure(conn)
        return bool(await conn.pub.sismember(key, member))

    async def sadd_ttl(self, key: str, member: str, ttl_s: float) -> None:
        """SADD + EXPIRE in one MULTI/EXEC transaction."""
        conn = self._conn()
        await self._ensure(conn)
        async with conn.pub.pipeline(transaction=True) as pipe:
            await pipe.sadd(key, member)
            await pipe.expire(key, int(ttl_s))
            await pipe.execute()

    async def srem(self, key: str, member: str) -> None:
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.srem(key, member)

    async def expire(self, key: str, ttl_s: float) -> None:
        conn = self._conn()
        await self._ensure(conn)
        await conn.pub.expire(key, int(ttl_s))

    async def subscribe(self, channel: str, handler: Callable[[bytes], None]) -> None:
        conn = self._conn()
        await self._ensure(conn)
        first = channel not in conn.handlers
        conn.handlers.setdefault(channel, []).append(handler)
        if first:
            await conn.sub.subscribe(channel)
        if conn.listener_task is None:
            conn.listener_task = asyncio.create_task(self._listener(conn))

    async def unsubscribe(self, channel: str) -> None:
        conn = self._conns.get(asyncio.get_running_loop())
        if conn is None or conn.sub is None:
            return
        conn.handlers.pop(channel, None)
        await conn.sub.unsubscribe(channel)

    async def _listener(self, conn: _LoopConn) -> None:
        while True:
            try:
                async for message in conn.sub.listen():
                    if message["type"] != "message":
                        continue
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode()
                    for handler in conn.handlers.get(channel, []):
                        try:
                            result = handler(message["data"])
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception:
                            logger.exception("Redis backplane handler error on %s", channel)
                # INVARIANT: exit the task once no channels remain subscribed; do NOT
                # reconnect. Why: with an empty handler set, sub.listen() ends instantly,
                # so the reconnect branch re-subscribes to nothing and listen() ends again
                # — a 5s busy-spin that never settles. That non-terminating task pins the
                # event loop and wedges pytest_asyncio's run_until_complete teardown
                # forever (CI hung 30+ min, no traceback). The next subscribe() respawns
                # the listener because we clear listener_task on the way out.
                if not conn.handlers:
                    conn.listener_task = None
                    return
                logger.warning("Redis backplane listener ended — reconnecting in 5s")
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Redis backplane listener crashed — reconnecting in 5s")
            await asyncio.sleep(5)
            try:
                await self._ensure(conn)
                for ch in list(conn.handlers.keys()):
                    await conn.sub.subscribe(ch)
            except Exception:
                logger.exception("Redis backplane reconnect failed — retrying in 5s")
                await asyncio.sleep(5)

    async def close(self) -> None:
        current = asyncio.get_running_loop()
        for loop, conn in list(self._conns.items()):
            if conn.listener_task is not None:
                conn.listener_task.cancel()
            # Only await teardown on the current loop; connections bound to other
            # (often already-closed) loops cannot be awaited here — drop them.
            if loop is current and not loop.is_closed():
                try:
                    if conn.listener_task is not None:
                        await conn.listener_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass  # WHY: best-effort teardown — a failing listener await must not mask the close path.
                try:
                    if conn.sub is not None:
                        await conn.sub.unsubscribe()
                        await conn.sub.aclose()
                    if conn.pub is not None:
                        await conn.pub.aclose()
                except Exception:
                    pass  # WHY: best-effort teardown — connections bound to closed loops can't be awaited here.
        self._conns.clear()


_backplane: RedisBackplane | None = None


def get_backplane() -> RedisBackplane:
    """Return the singleton Redis backplane (created on first call)."""
    global _backplane
    if _backplane is None:
        _backplane = RedisBackplane()
    return _backplane


def reset_backplane() -> None:
    """Reset the singleton — for testing only."""
    global _backplane
    _backplane = None
