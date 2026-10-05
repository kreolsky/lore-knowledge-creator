"""Guard test for the per-event-loop arq pool the test conftest installs.

A WS test opens a TestClient portal: a second event loop, in its own thread, beside the
session loop the async test runs on. One process-wide arq pool shared by both deadlocks
when the test blocks the session loop in a synchronous `ws.receive_text()` while a
session-loop task holds the redis ConnectionPool lock: the WS join's open-backup enqueue
on the portal waits on that lock until the zombie reaper closes the socket with 4008 after
90 s (observed in CI runs #1257, #1361, #1372).
"""
import pytest
from anyio.from_thread import start_blocking_portal


@pytest.mark.asyncio
async def test_a_second_event_loop_gets_its_own_arq_pool():
    from jobs import pool as jobs_pool

    session_loop_pool = await jobs_pool.get_arq_pool()
    with start_blocking_portal() as portal:
        portal_pool = portal.call(jobs_pool.get_arq_pool)
        shared = portal_pool is session_loop_pool
        # WHY guarded: closing a shared pool from the portal would close the session
        # loop's connections from the wrong loop — the cleanup is owed only to a pool the
        # portal itself created.
        if not shared:
            portal.call(jobs_pool.close_arq_pool)

    assert not shared
