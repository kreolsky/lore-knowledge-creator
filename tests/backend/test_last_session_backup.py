"""Tests for the per-user `last-session` backup.

# SYSTEM: last-session backup — one upserted checkpoint per (document, editor),
#   snapshotting the document state at the moment that editor's session ended.

Design (plan info-adaptive-boot.md):
- Reuses the `checkpoints` table with label `last-session`. NOT in AUTO_LABELS, so
  the GFS thinner never touches it (self-bounded to one row per user).
- Excluded from the adjacent-dup dedup scan (_latest_checkpoint_hash), so a freshly
  upserted last-session row cannot mask a genuine editor-handoff/content-loss backup.
- Written off the hot path via auto_backup_last_session_task.
- Triggered at editor-handoff, explicit leave, and idle-reap — gated on the leaving
  editor being in session._has_pushed (the "actually edited" set), NOT last_editor_id.
"""

from datetime import datetime, timedelta, timezone

import pytest
from auto_backup import LABEL_LAST_SESSION, _compute_hash
from collab.registry import _get_or_create_session
from cp_store import hash_content
from enqueue_recorder import EnqueueRecorder
from helpers import _DummyWS, set_session_text
from pycrdt import Doc, Text

# ─── upsert_last_session_backup (real DB) ────────────────────────────────────


def _ls_task_calls(mock_enqueue):
    return mock_enqueue.of("auto_backup_last_session_task")


@pytest.mark.asyncio
async def test_upsert_creates_then_updates_in_place(test_db):
    """First call creates one row; a second call for the SAME user UPDATES it in
    place (still one non-deleted row, created_at advanced, content refreshed)."""
    from auto_backup import upsert_last_session_backup
    from cp_store import get_content

    async def _blob_text(row):
        return await get_content(row["content_ref"])

    doc_id = "ls-doc-upsert"
    await upsert_last_session_backup(doc_id, "first content body", "user-A", "Alice")
    rows = await test_db.query(
        "SELECT meta::id(id) AS checkpoint_id, content_ref, created_by, created_at FROM checkpoints "
        "WHERE document_id = $did AND label = 'last-session' AND deleted_at IS NONE",
        {"did": doc_id},
    )
    assert len(rows) == 1
    first_id = rows[0]["checkpoint_id"]
    first_at = rows[0]["created_at"]

    await upsert_last_session_backup(doc_id, "second content body", "user-A", "Alice")
    rows = await test_db.query(
        "SELECT meta::id(id) AS checkpoint_id, content_ref, created_by, created_at FROM checkpoints "
        "WHERE document_id = $did AND label = 'last-session' AND deleted_at IS NONE",
        {"did": doc_id},
    )
    assert len(rows) == 1, "second call must UPDATE in place, not create a second row"
    assert rows[0]["checkpoint_id"] == first_id, "checkpoint_id must stay stable across upserts"
    assert await _blob_text(rows[0]) == "second content body"
    assert rows[0]["created_by"] == "user-A"
    assert rows[0]["created_at"] >= first_at


@pytest.mark.asyncio
async def test_upsert_separate_row_per_user(test_db):
    """Different users get distinct rows — X→Y→X keeps exactly one X row + one Y row."""
    from auto_backup import upsert_last_session_backup

    doc_id = "ls-doc-multi"
    await upsert_last_session_backup(doc_id, "X content", "user-X", "Xavier")
    await upsert_last_session_backup(doc_id, "Y content", "user-Y", "Yvonne")
    await upsert_last_session_backup(doc_id, "X content again", "user-X", "Xavier")

    rows = await test_db.query(
        "SELECT created_by FROM checkpoints WHERE document_id = $did AND label = 'last-session' AND deleted_at IS NONE",
        {"did": doc_id},
    )
    created_by = sorted(r["created_by"] for r in rows)
    assert created_by == ["user-X", "user-Y"], "exactly one row per user"


@pytest.mark.asyncio
async def test_upsert_skipped_for_reference(test_db):
    """References never produce a last-session backup (mirrors handoff INVARIANT)."""
    from auto_backup import upsert_last_session_backup

    doc_id = "ls-doc-ref"
    await upsert_last_session_backup(doc_id, "ref content", "user-A", "Alice", is_reference=True)
    rows = await test_db.query(
        "SELECT * FROM checkpoints WHERE document_id = $did AND label = 'last-session'",
        {"did": doc_id},
    )
    assert rows == []


# ─── Thinner immunity ────────────────────────────────────────────────────────


async def _seed_checkpoint(test_db, doc_id, suffix, label, ago, text):
    from db import create_record
    cp_id = f"ls-{doc_id}-{suffix}"
    await create_record("checkpoints", cp_id, {
        "document_id": doc_id,
        "content": text,
        "content_hash": hash_content(text),
        "content_ref": hash_content(text),
        "label": label,
        "created_at": datetime.now(timezone.utc) - ago,
    })
    return cp_id


@pytest.mark.asyncio
async def test_last_session_not_in_auto_labels():
    """last-session must stay OUT of AUTO_LABELS or the GFS thinner would soft-delete it."""
    from auto_backup import AUTO_LABELS
    assert LABEL_LAST_SESSION not in AUTO_LABELS


@pytest.mark.asyncio
async def test_thinner_does_not_delete_last_session(test_db):
    """A live last-session row survives thinning even when ancient (it's outside AUTO_LABELS)."""
    from jobs.tasks import thin_auto_checkpoints_task

    doc_id = "ls-doc-thin"
    await _seed_checkpoint(test_db, doc_id, "ls", "last-session", timedelta(days=120), "ls content")
    await _seed_checkpoint(test_db, doc_id, "auto", "auto-backup", timedelta(days=120), "auto content")

    await thin_auto_checkpoints_task({})

    rows = await test_db.query(
        "SELECT deleted_at FROM checkpoints WHERE document_id = $did AND label = 'last-session'",
        {"did": doc_id},
    )
    assert len(rows) == 1
    assert rows[0]["deleted_at"] is None


# ─── Dedup-exclusion: last-session must not mask a safety backup ─────────────


@pytest.mark.asyncio
async def test_dedup_scan_skips_last_session(test_db):
    """With a last-session row as the NEWEST checkpoint, _is_dup_of_latest must NOT
    treat its hash as the dedup target — a genuine editor-handoff/content-loss backup
    of identical content must still fire."""
    from auto_backup import _is_dup_of_latest

    doc_id = "ls-doc-dedup"
    shared_text = "shared identical content body"
    shared_hash = _compute_hash(shared_text)
    # last-session is the NEWEST row (ago=0) carrying shared_hash.
    await _seed_checkpoint(test_db, doc_id, "ls", "last-session", timedelta(0), shared_text)
    # An older auto-backup with a DIFFERENT hash proves the scan reads past last-session.
    await _seed_checkpoint(test_db, doc_id, "auto", "auto-backup", timedelta(hours=1), "older different content")

    db = test_db
    is_dup = await _is_dup_of_latest(db, doc_id, shared_hash)
    assert is_dup is False, "last-session row must be skipped by the adjacent-dup dedup scan"


@pytest.mark.asyncio
async def test_handoff_still_fires_when_last_session_is_newest(test_db):
    """End-to-end dedup-exclusion: a last-session row carrying identical content as the
    handoff snapshot must NOT suppress the editor-handoff backup."""
    from auto_backup import maybe_backup_on_editor_handoff

    doc_id = "ls-doc-handoff"
    shared_text = "Alice's long handoff content body that exceeds the handoff minimum length."
    await _seed_checkpoint(test_db, doc_id, "ls", "last-session", timedelta(0), shared_text)

    checkpoint, _ = await maybe_backup_on_editor_handoff(
        doc_id, shared_text, from_user_id="user-A", from_user_name="Alice",
    )
    assert checkpoint is not None, "editor-handoff backup must fire despite the identical last-session row"
    assert checkpoint["label"] == "editor-handoff"


# ─── Triggers: editor-handoff, leave, idle-reap (gated on _has_pushed) ───────


async def _edit_as(session, user_id, name, text, at=0, ws=None):
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
    await session.handle_binary_message(bytes([0, 2]) + diff, ws, is_multiplexed=False)
    return ws


@pytest.mark.asyncio
async def test_handoff_trigger_upserts_previous_editor(collab_project, _clear_sessions):
    """Editor change X→Y enqueues a last-session backup for the PREVIOUS editor (X)
    with the pre-edit content, alongside the existing editor-handoff backup."""
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Alice wrote this long content here.")
    session.last_editor_id = "user-A"
    session.last_editor_name = "Alice"
    session._has_pushed["user-A"] = None  # A has actually edited

    with EnqueueRecorder.active() as mock_enqueue:
        await _edit_as(session, "user-B", "Bob", "B")

    ls_calls = _ls_task_calls(mock_enqueue)
    assert len(ls_calls) == 1
    args, kwargs = ls_calls[0].args, ls_calls[0].kwargs
    assert args[0] == doc_id
    assert args[1] == "Alice wrote this long content here."  # pre-edit content attributed to A
    assert kwargs["editor_id"] == "user-A"
    assert kwargs["editor_name"] == "Alice"


@pytest.mark.asyncio
async def test_leave_trigger_upserts_editor(collab_project, _clear_sessions):
    """An editor (in _has_pushed) leaving enqueues their last-session backup."""
    from collab.join import leave_collab_session

    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Editor content before leaving.")
    ws = _DummyWS()
    session.add_client(ws, "user-A", "Alice", "full")
    session._has_pushed["user-A"] = None

    with EnqueueRecorder.active() as mock_enqueue:
        await leave_collab_session(ws, session, "user-A")

    ls_calls = _ls_task_calls(mock_enqueue)
    assert len(ls_calls) == 1
    args, kwargs = ls_calls[0].args, ls_calls[0].kwargs
    assert args[1] == "Editor content before leaving."
    assert kwargs["editor_id"] == "user-A"
    assert kwargs["editor_name"] == "Alice"


@pytest.mark.asyncio
async def test_reap_trigger_upserts_editor(collab_project, _clear_sessions):
    """Idle-reap of an editor (in _has_pushed) enqueues their last-session backup."""
    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Editor content before reap.")
    ws = _DummyWS()
    client = session.add_client(ws, "user-A", "Alice", "full")
    session._has_pushed["user-A"] = None
    # Make the client stale so _reap_zombie_clients picks it up.
    client.last_activity = 0.0

    with EnqueueRecorder.active() as mock_enqueue:
        await session._reap_zombie_clients()

    ls_calls = _ls_task_calls(mock_enqueue)
    assert len(ls_calls) == 1
    assert ls_calls[0].kwargs["editor_id"] == "user-A"


@pytest.mark.asyncio
async def test_non_editor_leave_no_backup(collab_project, _clear_sessions):
    """Leave of a viewer/commentator (NOT in _has_pushed) enqueues NO last-session
    backup — the attribution discriminator is _has_pushed, not 'any connected client'."""
    from collab.join import leave_collab_session

    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Some content.")
    ws = _DummyWS()
    session.add_client(ws, "viewer-V", "Viv", "readonly")  # never edited → not in _has_pushed

    with EnqueueRecorder.active() as mock_enqueue:
        await leave_collab_session(ws, session, "viewer-V")

    assert _ls_task_calls(mock_enqueue) == []


@pytest.mark.asyncio
async def test_editor_leave_after_handoff_no_second_upsert(collab_project, _clear_sessions):
    """If a previous editor leaves while a newer editor is active, only the leaving
    editor's last-session fires on the leave path (handoff already covered its
    snapshot). Gating on _has_pushed, not last_editor_id, is what makes this correct."""
    from collab.join import leave_collab_session

    _, doc_id, *_ = collab_project
    session = await _get_or_create_session("doc", doc_id, "")
    set_session_text(session, "Shared doc content.")
    ws_a = _DummyWS()
    session.add_client(ws_a, "user-A", "Alice", "full")
    session._has_pushed["user-A"] = None
    # B is now the latest editor; A is a still-connected PREVIOUS editor leaving.
    session.last_editor_id = "user-B"
    session.last_editor_name = "Bob"

    with EnqueueRecorder.active() as mock_enqueue:
        await leave_collab_session(ws_a, session, "user-A")

    ls_calls = _ls_task_calls(mock_enqueue)
    assert len(ls_calls) == 1
    assert ls_calls[0].kwargs["editor_id"] == "user-A"
