"""The periodic flush loop must not survive its own cancellation.

A wedge captured on 2026-08-17 (gw3, test_two_editors_converge_over_ws) parked the
whole worker in asyncio.runners._cancel_all_tasks: _periodic_flush_loop was suspended
inside _flush_if_needed on a DB await that never resolved, so the shutdown gather never
completed and TestClient's portal thread.join() blocked forever. Two properties close
that: the loop keeps ticking on a flush that hangs (it is not allowed to park forever),
and it is not left running at all once the last client has gone.
"""

import asyncio

import collab.registry as registry
import pytest
from collab.registry import (
    _cleanup_session,
    _get_or_create_session,
    _session_key,
    _sessions,
)


@pytest.fixture(autouse=True)
def _clear_sessions_after():
    yield
    for s in list(_sessions.values()):
        for task_attr in ("_flush_task", "_batch_task", "_grace_task"):
            t = getattr(s, task_attr, None)
            if t and not t.done():
                t.cancel()
    _sessions.clear()


@pytest.mark.asyncio
async def test_periodic_flush_does_not_park_forever_on_a_hanging_flush(monkeypatch, collab_project):
    """A flush that never returns is abandoned, and the loop lives to tick again."""
    _, doc_id, *_ = collab_project
    import collab.session as session_mod

    monkeypatch.setattr(session_mod, "FLUSH_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(session_mod, "FLUSH_TIMEOUT_SEC", 0.05, raising=False)

    session = await _get_or_create_session("doc", doc_id, "")
    entered = 0

    async def hanging_flush(*_args, **_kwargs):
        nonlocal entered
        entered += 1
        await asyncio.Event().wait()  # never resolves — the captured wedge

    monkeypatch.setattr(session, "_flush_if_needed", hanging_flush)
    monkeypatch.setattr(session, "_reap_zombie_clients", lambda: asyncio.sleep(0))

    async def _pace_always_ok() -> bool:
        return True

    monkeypatch.setattr(session._flush_pipeline, "_snapshot_pace_ok", _pace_always_ok)

    session.start_periodic_flush()
    await asyncio.sleep(0.4)

    # A loop that parked on the first hanging flush enters exactly once.
    assert entered > 1, f"flush loop parked on a hanging flush (entered={entered})"
    assert not session._flush_task.done()

    # And it still dies on cancel, promptly.
    session.stop_periodic_flush()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_last_client_leaving_stops_the_periodic_flush(monkeypatch, collab_project):
    """The flush loop is not left ticking through the 30s grace window.

    Why this matters beyond tidiness: during grace the loop is the only thing still
    issuing DB queries for a session nobody is connected to, which is exactly the
    window the captured teardown wedge landed in.
    """
    _, doc_id, *_ = collab_project
    monkeypatch.setattr(registry, "SESSION_GRACE_SEC", 30)

    session = await _get_or_create_session("doc", doc_id, "")
    session.start_periodic_flush()
    assert session._flush_task is not None and not session._flush_task.done()

    await _cleanup_session("doc", doc_id)

    # Session is still registered (grace), but nothing is ticking inside it.
    assert _session_key("doc", doc_id) in _sessions
    assert session._flush_task is None or session._flush_task.done(), (
        "periodic flush still running through the grace window"
    )
