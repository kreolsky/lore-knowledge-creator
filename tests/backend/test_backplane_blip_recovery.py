"""Gap #4 — backplane Redis-blip recovery.

reset_backplane() is a full singleton kill, not a transient blip — it does NOT
exercise the in-instance reconnect loop at backplane.py:_listener (which logs
"Redis backplane listener crashed — reconnecting in 5s"). This test drives that
reconnect path directly with a stub pubsub whose listen() crashes once (simulating
a server-side disconnect) then recovers, and asserts (a) the reconnect WARNING
fires and (b) a subsequent message is delivered to the handler (convergence).
"""

import asyncio
import logging

import pytest
from backplane import RedisBackplane


class _FakeSub:
    """Stub redis pubsub: listen() crashes on the first iteration, then yields."""

    def __init__(self, deliver: bytes):
        self._deliver = deliver
        self._calls = 0
        self.subscribed: list[str] = []

    def listen(self):
        outer = self

        async def _gen():
            if outer._calls == 0:
                outer._calls += 1
                raise ConnectionError("simulated redis blip")
            outer._calls += 1
            yield {"type": "message", "channel": "chan", "data": outer._deliver}

        return _gen()

    async def subscribe(self, *channels):
        self.subscribed.extend(channels)


@pytest.mark.asyncio
async def test_listener_recovers_after_blip_and_redelivers(monkeypatch, caplog):
    # Collapse the listener's 5s reconnect sleep to a single event-loop yield.
    # WHY save _real_sleep: backplane.asyncio IS the asyncio module, so patching
    # asyncio.sleep globally would also neuter this test's own wait below — the
    # saved reference keeps the original function for the test's polling loop.
    _real_sleep = asyncio.sleep

    async def _fast(_):
        await _real_sleep(0)
    monkeypatch.setattr("backplane.asyncio.sleep", _fast)

    bp = RedisBackplane()
    conn = bp._conn()  # _LoopConn with no real redis
    conn.pub = object()  # truthy → _ensure returns without touching real redis

    delivered: list[bytes] = []

    async def handler(data: bytes):
        delivered.append(data)

    conn.handlers["chan"] = [handler]
    conn.sub = _FakeSub(deliver=b"after-blip")

    with caplog.at_level(logging.WARNING, logger="backplane"):
        task = asyncio.create_task(bp._listener(conn))
        try:
            # Poll (using the REAL sleep) until the post-blip message lands.
            for _ in range(200):
                if delivered:
                    break
                await _real_sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    assert delivered and delivered[0] == b"after-blip", (
        "handler did not receive the post-blip message"
    )
    # The stub re-subscribed the channel on the reconnect branch.
    assert "chan" in conn.sub.subscribed
    # The reconnect logline fired (either the "ended" or "crashed" variant).
    assert any("reconnecting" in r.message for r in caplog.records)
