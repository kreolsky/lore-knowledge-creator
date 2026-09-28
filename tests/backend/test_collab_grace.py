"""Session idle grace period — a session lingers briefly after the last client leaves.

# WHY: Switching A→B→A used to tear the session down the instant the last client
# left, re-paying the full bootstrap (ydoc_store.load + CRDT replay + subscribe) on
# every reopen. The grace window makes a quick reopen free. See Fix 1 in the perf
# audit (plans/imperative-knitting-curry.md).
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
def _clear_grace_sessions():
    yield
    for s in list(_sessions.values()):
        for task_attr in ("_flush_task", "_batch_task", "_grace_task"):
            t = getattr(s, task_attr, None)
            if t and not t.done():
                t.cancel()
    _sessions.clear()


@pytest.mark.asyncio
async def test_session_lingers_after_last_client(monkeypatch, collab_project):
    """After the last client leaves, the session stays in the registry during grace."""
    _, doc_id, *_ = collab_project
    monkeypatch.setattr(registry, "SESSION_GRACE_SEC", 5)

    session = await _get_or_create_session("doc", doc_id, "")
    key = _session_key("doc", doc_id)
    assert key in _sessions

    # Simulate the last client leaving (clients already empty here).
    await _cleanup_session("doc", doc_id)

    # Still present during the grace window.
    assert key in _sessions
    assert _sessions[key] is session


@pytest.mark.asyncio
async def test_reopen_within_grace_reuses_session_no_reload(monkeypatch, collab_project):
    """Reopening within the grace window returns the SAME session — no second load()."""
    _, doc_id, *_ = collab_project
    monkeypatch.setattr(registry, "SESSION_GRACE_SEC", 5)

    import ydoc_store
    calls: list[str] = []
    real_load = ydoc_store.load

    async def spy_load(eid):
        calls.append(eid)
        return await real_load(eid)

    monkeypatch.setattr(ydoc_store, "load", spy_load)

    session = await _get_or_create_session("doc", doc_id, "")
    assert calls.count(doc_id) == 1

    await _cleanup_session("doc", doc_id)

    again = await _get_or_create_session("doc", doc_id, "")
    assert again is session
    # No reload happened on reopen within grace.
    assert calls.count(doc_id) == 1


@pytest.mark.asyncio
async def test_session_removed_after_grace_elapses(monkeypatch, collab_project):
    """Once the grace window elapses with no client, the session is torn down."""
    _, doc_id, *_ = collab_project
    monkeypatch.setattr(registry, "SESSION_GRACE_SEC", 0.1)

    await _get_or_create_session("doc", doc_id, "")
    key = _session_key("doc", doc_id)

    await _cleanup_session("doc", doc_id)
    assert key in _sessions  # still in grace

    await asyncio.sleep(0.25)
    assert key not in _sessions  # grace elapsed → gone


@pytest.mark.asyncio
async def test_reconnect_during_grace_cancels_teardown(monkeypatch, collab_project):
    """A reconnect during grace cancels the pending teardown; the session survives
    past the original grace deadline with its backplane subscription intact."""
    _, doc_id, *_ = collab_project
    monkeypatch.setattr(registry, "SESSION_GRACE_SEC", 0.2)

    session = await _get_or_create_session("doc", doc_id, "")
    key = _session_key("doc", doc_id)

    await _cleanup_session("doc", doc_id)
    assert key in _sessions

    # Reconnect before the grace timer fires.
    again = await _get_or_create_session("doc", doc_id, "")
    assert again is session

    # Wait past the original grace deadline.
    await asyncio.sleep(0.3)

    # Teardown must have been cancelled — session still alive, backplane intact.
    assert key in _sessions
    assert session._backplane_subscribed is True
