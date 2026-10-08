"""Tests for editor-handoff auto-backup wiring on the collab push path.

The handoff snapshot fires the first time each distinct user edits in a live
session, only if a different previous editor exists. The snapshot string is
captured synchronously on the push path (pre-edit content) and the actual
backup write is enqueued to the arq worker (auto_backup_handoff_task).

Drives the real binary path via handle_binary_message; mocks jobs.pool.enqueue.
"""

import json
import time

import pytest
from collab.registry import _get_or_create_session, _session_key, _sessions
from collab.session import _HAS_PUSHED_CAP
from collab.sync import MSG_SYNC, MSG_SYNC_UPDATE, wrap_binary
from enqueue_recorder import EnqueueRecorder
from helpers import _DummyWS, set_session_text
from pycrdt import Doc, Text


async def _edit_as(session, user_id: str, name: str, text: str, at: int = 0, ws=None):
    """Apply an edit through the real binary path attributed to a specific user."""
    if ws is None:
        ws = _DummyWS()
    if id(ws) not in session.clients:
        session.add_client(ws, user_id, name, "full")
    replica = Doc()
    replica.apply_update(session.ydoc.get_update())
    state_before = session.ydoc.get_state()
    replica.get("content", type=Text).insert(at, text)
    diff = replica.get_update(state_before)
    # MSG_SYNC=0, MSG_SYNC_UPDATE=2
    await session.handle_binary_message(bytes([0, 2]) + diff, ws, is_multiplexed=False)
    return ws


def _handoff_calls(mock_enqueue):
    return mock_enqueue.of("auto_backup_handoff_task")


@pytest.mark.asyncio
async def test_handoff_enqueued_on_user_switch(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice wrote this long content here.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active() as mock_enqueue:
        await _edit_as(session, "user-B", "Bob", "B")

    calls = _handoff_calls(mock_enqueue)
    assert len(calls) == 1
    args, kwargs = calls[0].args, calls[0].kwargs
    assert args[0] == doc_id
    assert args[1] == "Alice wrote this long content here."  # pre-edit content
    assert kwargs["from_user_id"] == "user-A"


@pytest.mark.asyncio
async def test_no_handoff_same_user(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice's existing content here.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active() as mock_enqueue:
        await _edit_as(session, "user-A", "Alice", "more")

    assert _handoff_calls(mock_enqueue) == []
    assert session.last_editor_id == "user-A"


@pytest.mark.asyncio
async def test_no_handoff_second_push_same_session(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice baseline content for handoff.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active() as mock_enqueue:
        ws_b = await _edit_as(session, "user-B", "Bob", "B1")
        await _edit_as(session, "user-B", "Bob", "B2", ws=ws_b)

    assert len(_handoff_calls(mock_enqueue)) == 1


@pytest.mark.asyncio
async def test_no_handoff_for_reference(collab_ref_project, _clear_sessions):
    _, _, ref_id, *_ = collab_ref_project
    session = await _get_or_create_session("doc", ref_id, "", entity={"is_reference": True})
    assert session.is_reference is True
    set_session_text(session, "Reference media content goes here.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active() as mock_enqueue:
        await _edit_as(session, "user-B", "Bob", "B")

    assert _handoff_calls(mock_enqueue) == []


@pytest.mark.asyncio
async def test_last_editor_updates_on_push(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice baseline.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active():
        await _edit_as(session, "user-B", "Bob", "B")

    assert session.last_editor_id == "user-B"
    assert session.last_editor_name == "Bob"


@pytest.mark.asyncio
async def test_handoff_after_external_change_rearms(collab_project, _clear_sessions):
    _, doc_id, *_ = collab_project
    from collab.events import apply_external_content_change

    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice baseline content for rearm test.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"

    with EnqueueRecorder.active() as mock_enqueue:
        await _edit_as(session, "user-B", "Bob", "B")  # handoff #1 (prev A)
        await _edit_as(session, "user-C", "Carol", "C")  # handoff #2 (prev B)
        assert len(_handoff_calls(mock_enqueue)) == 2

        # External content change (e.g. checkpoint restore) re-arms handoff.
        await apply_external_content_change(
            "doc", doc_id, "Restored content here.", already_persisted=True,
        )
        assert session._has_pushed == {}

        await _edit_as(session, "user-B", "Bob", "B again")  # handoff #3 (prev C)
        assert len(_handoff_calls(mock_enqueue)) == 3


@pytest.mark.asyncio
async def test_no_handoff_when_has_pushed_cap_reached(collab_project, _clear_sessions):
    """At the _has_pushed cap an untracked user fails CLOSED — no handoff, and it
    does not re-fire on subsequent pushes (the once-per-session guarantee holds)."""
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice baseline content at cap edge.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"
    # Saturate the per-session set so a new user cannot be recorded.
    session._has_pushed = {f"u{i}": None for i in range(_HAS_PUSHED_CAP)}

    with EnqueueRecorder.active() as mock_enqueue:
        ws = await _edit_as(session, "user-Z", "Zoe", "Z1")
        await _edit_as(session, "user-Z", "Zoe", "Z2", ws=ws)

    assert _handoff_calls(mock_enqueue) == []
    # last_editor still tracks the actual latest editor.
    assert session.last_editor_id == "user-Z"


# ─── End-to-end: two users over the real project collab WebSocket ─────────────
# The automatable half of the two-browser manual check (plan §Verification #5).
# Drives two LIVE WS clients through the actual multiplexed route and asserts the
# handoff snapshot is enqueued for the *previous* editor on the second user's first
# edit. The actual checkpoint write happens in the arq worker (not running in tests),
# so we assert the enqueue — the wire wiring up to that point is what's exercised.


def _join(ws, doc_id):
    ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))


def _recv_until(ws, *, want_type=None, limit=10):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("text") is not None:
            parsed = json.loads(msg["text"])
            if want_type and parsed.get("type") == want_type:
                return parsed
    raise AssertionError(f"frame not seen (type={want_type})")


def _edit_frame(session, doc_id, text, at=0):
    """Build a multiplexed Yjs SYNC_UPDATE frame for `text` against the live session."""
    replica = Doc()
    replica.apply_update(session.ydoc.get_update())
    state_before = session.ydoc.get_state()
    replica.get("content", type=Text).insert(at, text)
    diff = replica.get_update(state_before)
    inner = bytes([MSG_SYNC, MSG_SYNC_UPDATE]) + diff
    return wrap_binary(doc_id, MSG_SYNC, inner)


def _wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.usefixtures("_clear_sessions")
class TestHandoffE2E:
    def test_two_user_switch_enqueues_handoff_for_first_editor(self, sync_app, collab_project):
        pid, doc_id, admin_token, user_token, admin_uid, user_uid = collab_project

        with EnqueueRecorder.active() as mock_enqueue:
            with sync_app.websocket_connect(
                f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
            ) as red, sync_app.websocket_connect(
                f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
            ) as black:
                _join(red, doc_id)
                _recv_until(red, want_type="init")
                _join(black, doc_id)
                _recv_until(black, want_type="init")

                session = _sessions[_session_key("doc", doc_id)]

                # red (admin) edits first → becomes last_editor, no handoff yet.
                red.send_bytes(_edit_frame(session, doc_id, "RED-EDIT "))
                assert _wait_until(lambda: session.last_editor_id == admin_uid)
                assert _handoff_calls(mock_enqueue) == []

                # black (user) makes the first edit of a NEW editor → handoff for red.
                red_content = session.content
                black.send_bytes(_edit_frame(session, doc_id, "BLACK-EDIT "))
                assert _wait_until(lambda: len(_handoff_calls(mock_enqueue)) == 1)

            calls = _handoff_calls(mock_enqueue)
            assert len(calls) == 1
            args, kwargs = calls[0].args, calls[0].kwargs
            assert args[0] == doc_id
            # Snapshot is red's pre-edit content (before black's edit was applied).
            assert args[1] == red_content
            assert "BLACK-EDIT" not in args[1]
            assert kwargs["from_user_id"] == admin_uid
