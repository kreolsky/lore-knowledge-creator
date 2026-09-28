"""Gap #3 — zombie client reap removes the client, enqueues a backup for editors
(not viewers), and is observable.

The plan's note that "no DB telemetry row is written" is stale: session.py:223
records a `collab/server-reap` telemetry row via telemetry_store. This test
asserts the durable observables: client gone from session.clients, a
last-session backup enqueued ONLY for an editor, the reap WARNING logged, and
the server-reap telemetry row recorded. Best-effort `_safe_ws_close` /
`send_bytes` silent-`pass` sites on this path are documented in B2.
"""

import logging
import time
from unittest.mock import AsyncMock

import pytest
from collab.session import HEARTBEAT_TIMEOUT_SEC, CollabSession, ConnectedClient
from enqueue_recorder import EnqueueRecorder

_built_sessions: list[CollabSession] = []


def _mk_session(entity_id="d1", is_reference=False) -> CollabSession:
    from pycrdt import Doc, Text
    doc = Doc()
    text = doc.get("content", type=Text)
    text += "baseline content"
    doc._lore_text = text
    session = CollabSession(
        entity_type="doc", entity_id=entity_id, ydoc=doc, is_reference=is_reference,
    )
    # The reap ends in queue_broadcast("user_left"), which spawns _flush_batch on the
    # SESSION-scoped loop. These sessions are never registered in _sessions, so the
    # registry teardown cannot reach that task — track them here instead.
    _built_sessions.append(session)
    return session


@pytest.fixture(autouse=True)
def _stop_built_sessions():
    """Cancel the background loops of every session this module built."""
    _built_sessions.clear()
    yield
    for session in _built_sessions:
        session.stop_periodic_flush()
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
    _built_sessions.clear()


def _stale_client(user_id, name, access="full", pushed=True) -> ConnectedClient:
    ws = AsyncMock()
    client = ConnectedClient(ws=ws, user_id=user_id, user_name=name, access_level=access)
    # Force last_activity well past the heartbeat timeout.
    client.last_activity = time.monotonic() - (HEARTBEAT_TIMEOUT_SEC + 30)
    return client


@pytest.fixture(autouse=True)
def _capture_telemetry(monkeypatch):
    recorded = []
    async def _record(events):
        recorded.extend(events)
    monkeypatch.setattr("telemetry_store.record_telemetry_events", _record)
    return recorded


@pytest.mark.asyncio
async def test_reap_removes_editor_and_enqueues_backup(caplog, _capture_telemetry):
    session = _mk_session()
    editor = _stale_client("u-editor", "Editor", access="full")
    session.clients[id(editor.ws)] = editor
    session._has_pushed["u-editor"] = None  # has edited → eligible for backup
    session.last_editor_id = "u-editor"
    session.last_editor_name = "Editor"

    with EnqueueRecorder.active() as enqueued, \
         caplog.at_level(logging.WARNING, logger="collab.session"):
        await session._reap_zombie_clients()

    assert id(editor.ws) not in session.clients
    # Editor (a pusher) → last-session backup enqueued.
    assert "auto_backup_last_session_task" in enqueued.names()
    # Observable: the reap WARNING + a server-reap telemetry row.
    assert any("Reaping zombie WS" in r.message for r in caplog.records)
    assert any(
        e.get("kind") == "server-reap" and e.get("user_id") == "u-editor"
        for e in _capture_telemetry
    )


@pytest.mark.asyncio
async def test_reap_skips_backup_for_viewer(caplog, _capture_telemetry):
    session = _mk_session()
    viewer = _stale_client("u-viewer", "Viewer", access="readonly")
    session.clients[id(viewer.ws)] = viewer
    # Viewer never pushed → must NOT be in _has_pushed.

    with EnqueueRecorder.active() as enqueued, \
         caplog.at_level(logging.WARNING, logger="collab.session"):
        await session._reap_zombie_clients()

    assert id(viewer.ws) not in session.clients
    assert not enqueued.calls  # no backup path fires for a non-editor
