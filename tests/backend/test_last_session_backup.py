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


import pytest
from collab.registry import _get_or_create_session
from enqueue_recorder import EnqueueRecorder
from helpers import _DummyWS, set_session_text

# ─── upsert_last_session_backup (real DB) ────────────────────────────────────


def _ls_task_calls(mock_enqueue):
    return mock_enqueue.of("auto_backup_last_session_task")


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


# ─── Triggers: editor-handoff, leave, idle-reap (gated on _has_pushed) ───────


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
