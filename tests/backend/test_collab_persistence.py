"""Tests for collab session persistence — dirty flag, periodic flush, shutdown.

CRDT: edits ride the pycrdt Y.Doc (no OT push). Tests drive the session via
the real binary path (apply_binary_edit) or by mutating the derived Y.Text,
then assert the flush pipeline persists derived content to documents.content.
"""


import pytest
from collab.registry import _get_or_create_session, _session_key, _sessions
from helpers import apply_binary_edit
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
