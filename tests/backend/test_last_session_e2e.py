"""E2E: last-session + editor-handoff backups fire over the real multiplexed project WS.

Two LIVE WS clients (red=admin, black=user) edit, switch editor, and leave through
/ws/collab/project/{pid}. The arq worker is NOT running under pytest, so jobs.pool.enqueue
is patched to CAPTURE the enqueued jobs; the captured task functions are then executed
against the test DB to assert the checkpoints are actually written — the wire wiring
all the way to a persisted row.

Covers the two backup triggers the feature adds:
  1. editor switch (handoff) → the PREVIOUS editor gets an editor-handoff safety
     backup AND a per-user last-session backup (from the pre-edit content).
  2. session end (explicit leave) → the LEAVING editor gets a last-session backup.
"""

import json
import time

import pytest
from collab.registry import _session_key, _sessions
from collab.sync import MSG_SYNC, MSG_SYNC_UPDATE, wrap_binary
from helpers import set_session_text
from pycrdt import Doc, Text


def _join(ws, doc_id):
    ws.send_text(json.dumps({"type": "join", "entity_type": "doc", "entity_id": doc_id}))


def _recv_until(ws, *, want_type=None, limit=10):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("text") is not None:
            parsed = json.loads(msg["text"])
            if want_type is None or parsed.get("type") == want_type:
                return parsed
    raise AssertionError(f"frame not seen (type={want_type})")


def _edit_frame(session, doc_id, text, at=0):
    """Build a multiplexed Yjs SYNC_UPDATE frame inserting `text` against the live doc."""
    replica = Doc()
    replica.apply_update(session.ydoc.get_update())
    state_before = session.ydoc.get_state()
    replica.get("content", type=Text).insert(at, text)
    diff = replica.get_update(state_before)
    inner = bytes([MSG_SYNC, MSG_SYNC_UPDATE]) + diff
    return wrap_binary(doc_id, MSG_SYNC, inner)


def _wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.mark.usefixtures("_clear_sessions")
class TestLastSessionE2E:
    @pytest.mark.asyncio
    async def test_editor_switch_and_leave_create_backups(
        self, sync_app, collab_project, test_db, enqueue_recorder,
    ):
        """Switching editor upserts the previous editor's backups; leaving upserts the
        leaving editor's last-session — and the checkpoints actually land in the DB."""
        pid, doc_id, admin_token, user_token, admin_uid, user_uid = collab_project

        # Seed long-enough initial content so the handoff snapshot clears
        # HANDOFF_MIN_CONTENT (50) but stays under BACKUP_MIN_CONTENT (200) so the
        # safety-open backup does NOT fire on join (would otherwise dedup-mask the
        # handoff and muddy the assertion).
        session = await _get_or_create(doc_id)
        set_session_text(session, "Initial document body for the e2e handoff test here.")

        with sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": admin_token}
        ) as ws_red, sync_app.websocket_connect(
            f"/ws/collab/project/{pid}", cookies={"lore_session": user_token}
        ) as ws_black:
            _join(ws_red, doc_id)
            _recv_until(ws_red, want_type="init")
            _join(ws_black, doc_id)
            _recv_until(ws_black, want_type="init")

            session = _sessions[_session_key("doc", doc_id)]

            # red edits first → becomes last_editor, recorded in _has_pushed.
            ws_red.send_bytes(_edit_frame(session, doc_id, "RED-EDIT-"))
            assert _wait_until(lambda: session.last_editor_id == admin_uid), "red's edit not applied"
            assert _wait_until(lambda: admin_uid in session._has_pushed)

            # black's first edit → editor switch: handoff + last-session for red.
            red_content = session.content
            ws_black.send_bytes(_edit_frame(session, doc_id, "BLACK-EDIT-"))
            assert _wait_until(
                lambda: len(enqueue_recorder.of("auto_backup_handoff_task")) >= 1
            ), "handoff backup not enqueued on editor switch"
            assert _wait_until(
                lambda: any(
                    c.kwargs.get("editor_id") == admin_uid
                    for c in enqueue_recorder.of("auto_backup_last_session_task")
                )
            ), "red's last-session not enqueued on editor switch"

            # black leaves the entity → last-session for black.
            ws_black.send_text(json.dumps({"type": "leave", "entity_id": doc_id}))
            assert _wait_until(
                lambda: any(
                    c.kwargs.get("editor_id") == user_uid
                    for c in enqueue_recorder.of("auto_backup_last_session_task")
                )
            ), "black's last-session not enqueued on leave"

            # --- simulate the arq worker: execute the captured backup tasks ---
            # Done INSIDE the WS block so red is still connected — otherwise red's
            # WS-disconnect (on with-exit) would enqueue a second last-session for
            # red with the merged live content, overwriting the handoff snapshot.
            from jobs.tasks import (
                auto_backup_handoff_task,
                auto_backup_last_session_task,
            )

            for c in enqueue_recorder.calls:
                kwargs = {k: v for k, v in c.kwargs.items() if k != "job_id"}
                if c.name == "auto_backup_handoff_task":
                    await auto_backup_handoff_task({}, *c.args, **kwargs)
                elif c.name == "auto_backup_last_session_task":
                    await auto_backup_last_session_task({}, *c.args, **kwargs)

            # --- assert the checkpoints actually persisted ---
            # Content lives only in the content_ref blob (INVARIANT C3-A), so
            # resolve each row's text via the blob instead of the nulled inline
            # column.
            from cp_store import get_content

            async def _text(row):
                return await get_content(row["content_ref"])

            rows = await test_db.query(
                "SELECT meta::id(id) AS cpid, label, created_by, content_ref, content_hash "
                "FROM checkpoints WHERE document_id = $did AND deleted_at IS NONE",
                {"did": doc_id},
            )
            by_key = {(r["label"], r["created_by"]): r for r in rows}

            # editor-handoff backup attributed to red (the previous editor), from the
            # doc state BEFORE black's edit — deterministic (handoff uses pre_content).
            assert ("editor-handoff", admin_uid) in by_key, f"red handoff missing: {set(by_key)}"
            handoff_row = by_key[("editor-handoff", admin_uid)]
            handoff_text = await _text(handoff_row)
            assert handoff_text == red_content
            assert "BLACK-EDIT" not in handoff_text
            # red's per-user last-session row, from red's pre-switch content.
            assert ("last-session", admin_uid) in by_key, f"red last-session missing: {set(by_key)}"
            assert await _text(by_key[("last-session", admin_uid)]) == red_content
            # black's per-user last-session row, created on leave (merged live content).
            assert ("last-session", user_uid) in by_key, f"black last-session missing: {set(by_key)}"
            assert "BLACK-EDIT" in await _text(by_key[("last-session", user_uid)])
            # exactly one last-session row per editor (the upsert coalesces).
            ls_editors = sorted(
                r["created_by"] for r in rows if r["label"] == "last-session"
            )
            assert ls_editors == [admin_uid, user_uid], \
                f"expected one last-session per editor, got {ls_editors}"


async def _get_or_create(doc_id):
    from collab.registry import _get_or_create_session
    return await _get_or_create_session("doc", doc_id, "")
