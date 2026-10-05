"""Tests for collab session persistence — dirty flag, periodic flush, shutdown.

CRDT: edits ride the pycrdt Y.Doc (no OT push). Tests drive the session via
the real binary path (apply_binary_edit) or by mutating the derived Y.Text,
then assert the flush pipeline persists derived content to documents.content.
"""

import asyncio

import pytest
from collab.registry import _get_or_create_session, _session_key, _sessions
from helpers import _DummyWS, apply_binary_edit, set_session_text
from pycrdt import Doc, Text

from db import get_db


def _drop_session(doc_id: str) -> None:
    """Simulate a server restart: pop the session and stop its tasks — a popped
    session is invisible to the _clear_sessions teardown, so it would leak."""
    session = _sessions.pop(_session_key("doc", doc_id), None)
    if session is None:
        return
    session.stop_periodic_flush()
    if session._batch_task and not session._batch_task.done():
        session._batch_task.cancel()


async def _db_content(doc_id: str) -> str | None:
    db = await get_db()
    rows = await db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": doc_id}
    )
    return rows[0]["content"] if rows else None


@pytest.mark.asyncio
async def test_binary_edit_sets_dirty_flag(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    assert session._dirty is False
    await apply_binary_edit(session, "test", at=0)
    assert session._dirty is True


@pytest.mark.asyncio
async def test_flush_clears_dirty_flag(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    await apply_binary_edit(session, "x", at=0)
    assert session._dirty is True

    await session._flush_if_needed(force=True)
    assert session._dirty is False


@pytest.mark.asyncio
async def test_flush_persists_content_to_db(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    set_session_text(session, "Persisted!")
    session._dirty = True

    await session.flush_to_db()

    assert await _db_content(doc_id) == "Persisted!"


@pytest.mark.asyncio
async def test_periodic_flush_task_starts_and_stops(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    session.start_periodic_flush()
    assert session._flush_task is not None
    assert not session._flush_task.done()

    session.stop_periodic_flush()
    assert session._flush_task is None


@pytest.mark.asyncio
async def test_periodic_flush_flushes_idle_dirty_session(
    collab_project, _clear_sessions, monkeypatch
):
    """Backend periodic flush covers 'user idle but session dirty' — no client flush needed.

    Pacing note: the periodic loop offers a flush only when
    FlushPipeline._snapshot_pace_ok() is True. For a session with no successful
    flush yet the gate is OPEN (leading edge — first edit flushes on the next
    tick), so this test pins FLUSH_SNAPSHOT_MIN_INTERVAL_SEC high to make that
    dependency explicit instead of accidental.
    """
    monkeypatch.setattr(
        "config.FLUSH_SNAPSHOT_MIN_INTERVAL_SEC", 3600.0
    )
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    session.start_periodic_flush()
    try:
        await apply_binary_edit(session, "idle-flush-test", at=0)
        assert session._dirty is True

        await asyncio.sleep(1.5)  # FLUSH_INTERVAL_SEC = 1 (loop tick)

        assert session._dirty is False, "Periodic flush did not clear dirty flag within 1.5s"
    finally:
        session.stop_periodic_flush()

    assert await _db_content(doc_id) == "idle-flush-testHello world"


# --- CRDT re-seed duplication regression ---------------------------------
# Bug: editing a document then re-entering duplicated its text (×2, ×3, …),
# copies concatenated. Root cause: load() re-encoded documents.content as a
# fresh Y.Doc with a RANDOM client_id whenever ydoc_state was absent, and
# ydoc_state was never persisted. The same text under two client_ids merges
# into duplicated content on reconnect. See lessons/2026-05-30-*.


async def _ydoc_state(doc_id: str) -> bytes | None:
    db = await get_db()
    rows = await db.query(
        "SELECT ydoc_state FROM type::record('documents', $id)", {"id": doc_id}
    )
    return rows[0].get("ydoc_state") if rows else None


@pytest.mark.asyncio
async def test_flush_persists_ydoc_state(collab_project, _clear_sessions):
    """flush_to_db must persist the CRDT snapshot, not just derived content.

    Without a persisted snapshot, the next load re-seeds from plaintext and
    duplicates content on reconnect.
    """
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "Hello world")
    set_session_text(session, "Persisted!")
    session._dirty = True

    await session.flush_to_db()

    state = await _ydoc_state(doc_id)
    assert state is not None, "flush did not persist ydoc_state"
    rebuilt = Doc()
    rebuilt.apply_update(state)
    assert str(rebuilt.get("content", type=Text)) == "Persisted!"


@pytest.mark.asyncio
async def test_cold_seed_is_deterministic_no_duplication(collab_project, _clear_sessions):
    """Two cold seeds of the same content (no ydoc_state) must be idempotent.

    Covers the reconnect window before the first flush: a server re-seed and a
    client holding the same content must merge to a single copy.
    """
    _, doc_id, *_ = collab_project
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c, ydoc_state = NONE",
        {"id": doc_id, "c": "Hello world"},
    )
    from ydoc_store import load

    a = await load(doc_id)
    b = await load(doc_id)
    a.apply_update(b.get_update())
    assert str(a.get("content", type=Text)) == "Hello world"


@pytest.mark.asyncio
async def test_reconnect_after_edit_does_not_duplicate(collab_project, _clear_sessions):
    """The reported bug: edit a doc, re-enter → text must NOT triple.

    Simulates a full edit→flush→server-restart→reconnect cycle and asserts the
    reconnecting client converges to a single copy of the content.
    """
    _, doc_id, *_ = collab_project
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c, ydoc_state = NONE",
        {"id": doc_id, "c": "Hello"},
    )

    # Session 1: a client syncs from the server seed, then edits.
    session = await _get_or_create_session("doc", doc_id, "Hello")
    ws1 = _DummyWS()
    session.add_client(ws1, "test-user", "Test", "full")
    client = Doc()
    client.apply_update(session.ydoc.get_update())
    client.get("content", type=Text).insert(5, " world")
    diff = client.get_update(session.ydoc.get_state())
    await session.handle_binary_message(bytes([0, 2]) + diff, ws1, is_multiplexed=False)
    await session.flush_to_db()
    assert await _db_content(doc_id) == "Hello world"

    # Server "restart" / re-enter: drop the in-memory session.
    _drop_session(doc_id)

    # Reconnect: a fresh server session loads from the store; the same client
    # (still holding "Hello world") asks for any items it is missing.
    session2 = await _get_or_create_session("doc", doc_id, await _db_content(doc_id))
    catch_up = session2.ydoc.get_update(client.get_state())
    client.apply_update(catch_up)

    assert str(client.get("content", type=Text)) == "Hello world"
    assert str(session2.ydoc.get("content", type=Text)) == "Hello world"


@pytest.mark.asyncio
async def test_multiplexed_update_is_applied(collab_project, _clear_sessions):
    """Regression: a multiplexed SYNC_UPDATE must reach the Y.Doc.

    The frontend wraps the full Yjs message ([MSG_SYNC, SYNC_UPDATE, ...update])
    inside the entity envelope. The handler must pass the unwrapped payload
    through unchanged — prepending the protocol byte again shifts the sync
    subtype, so the update is misread as a STEP1 and silently dropped, and the
    edit never persists (content stays empty). See lessons/2026-05-30.
    """
    from collab.sync import MSG_SYNC, MSG_SYNC_UPDATE, wrap_binary

    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "")  # start empty regardless of fixture content
    session._last_flushed_content = ""

    replica = Doc()
    replica.apply_update(session.ydoc.get_update())
    sv = session.ydoc.get_state()
    replica.get("content", type=Text).insert(0, "Hello")
    update = replica.get_update(sv)

    sync_msg = bytes([MSG_SYNC, MSG_SYNC_UPDATE]) + update
    frame = wrap_binary(doc_id, MSG_SYNC, sync_msg)
    ws2 = _DummyWS()
    session.add_client(ws2, "test-user", "Test", "full")
    await session.handle_binary_message(frame, ws2, is_multiplexed=True)

    assert session.content == "Hello"
    assert session._dirty is True


# --- set_content(persist=True) ---------------------------------------------
# Bug: REST apply paths (agent, checkpoints, files, transcription) called
# set_content() but never persisted ydoc_state, causing data loss on next
# load when no collab session was active. The persist=True flag consolidates
# the DB write and prunes stale update log entries.


async def _ydoc_updates_count(doc_id: str) -> int:
    db = await get_db()
    rows = await db.query(
        "SELECT count() AS total FROM ydoc_updates WHERE document_id = $id GROUP ALL",
        {"id": doc_id},
    )
    return rows[0].get("total", 0) if rows else 0


@pytest.mark.asyncio
async def test_set_content_persist_writes_ydoc_state(collab_project, _clear_sessions):
    """set_content(persist=True) must write both content and ydoc_state to DB."""
    _, doc_id, *_ = collab_project
    from ydoc_store import set_content

    await set_content(doc_id, "Persisted via REST", persist=True)

    assert await _db_content(doc_id) == "Persisted via REST"
    state = await _ydoc_state(doc_id)
    assert state is not None, "persist=True did not write ydoc_state"
    rebuilt = Doc()
    rebuilt.apply_update(state)
    assert str(rebuilt.get("content", type=Text)) == "Persisted via REST"


@pytest.mark.asyncio
async def test_set_content_persist_prunes_update_log(collab_project, _clear_sessions):
    """set_content(persist=True) must DELETE stale ydoc_updates."""
    _, doc_id, *_ = collab_project
    from ydoc_store import set_content

    session = await _get_or_create_session("doc", doc_id, "Hello")
    await apply_binary_edit(session, "edit", at=0)
    await session.flush_to_db()
    assert await _ydoc_updates_count(doc_id) >= 1

    _drop_session(doc_id)
    await set_content(doc_id, "Fresh content", persist=True)

    assert await _ydoc_updates_count(doc_id) == 0


@pytest.mark.asyncio
async def test_load_after_set_content_persist_returns_correct_content(collab_project, _clear_sessions):
    """After set_content(persist=True), load() must return the new content."""
    _, doc_id, *_ = collab_project
    from ydoc_store import load, set_content

    await set_content(doc_id, "New content", persist=True)
    _drop_session(doc_id)

    doc = await load(doc_id)
    assert str(doc.get("content", type=Text)) == "New content"


@pytest.mark.asyncio
async def test_set_content_without_persist_does_not_touch_db(collab_project, _clear_sessions):
    """set_content(persist=False) must NOT change DB content or ydoc_state."""
    _, doc_id, *_ = collab_project
    from ydoc_store import set_content

    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c, ydoc_state = NONE",
        {"id": doc_id, "c": "Original"},
    )

    await set_content(doc_id, "Changed in memory only")

    assert await _db_content(doc_id) == "Original"
    assert await _ydoc_state(doc_id) is None
