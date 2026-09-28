"""Regression tests for the selection-region-agent review-fix plan.

Covers the backend fixes that unit tests bypassed when the feature first landed
(the D1 REST /proposals-apply body regression was deleted with the proposal
cluster — mid-turn approval replaced that endpoint):

* D2 — WARNING: a surgical edit whose range shifted between the resolve and the
  `_write_lock` write MUST raise `AppliedUnverifiedError` (fail-stop) instead of
  deleting the WRONG byte range (corruption).
* D3 — WARNING: `apply_external_content_change(..., already_persisted=True)`
  must leave `session._dirty is False` — the post-branch unconditional
  `session._dirty = True` overrode the wholesale `already_persisted` reset.

# SYSTEM: chat-region-containment-tests — review-fix regressions.
"""

import pytest
from helpers import join_collab_ws, project_collab_url, run_sync


@pytest.fixture
def _clear_sessions():
    from collab.registry import _sessions

    _sessions.clear()
    yield
    for session in _sessions.values():
        session.stop_periodic_flush()
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
    _sessions.clear()


def _set_session_text(session, text: str) -> None:
    t = session._get_text()
    old_len = len(t)
    if old_len > 0:
        del t[0:old_len]
    if text:
        t += text


# ─── D2: surgical race guard (fail-stop, no corruption) ───────────────────────


class TestSurgicalRaceGuard:
    """D2: when `original_text` is supplied and the live slice moved between the
    resolve (outside `_write_lock`) and the surgical write (inside), the splice
    MUST raise `AppliedUnverifiedError` instead of corrupting the doc."""

    def test_surgical_raises_when_range_shifted(
        self, sync_app, collab_project, _clear_sessions
    ):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions
        from textmatch import AppliedUnverifiedError

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            # "hello world": old_string "world" resolves to cp[6,11) = "world".
            # Simulate a concurrent edit shifting the text BEFORE the surgical
            # write: original_text="world" but the live slice [6,11) is now "WORLD"
            # (changed) — the guard must detect the shift and raise.
            _set_session_text(session, "hello WORLD")
            with pytest.raises(AppliedUnverifiedError):
                run_sync(
                    apply_external_content_change(
                        "doc", doc_id,
                        from_cp=6, to_cp=11, new_text="earth",
                        original_text="world",  # what the resolver saw, now stale
                    )
                )
            # Fail-stop: the doc was NOT mutated by the stale write.
            assert session.content == "hello WORLD"

    def test_surgical_applies_when_range_matches(
        self, sync_app, collab_project, _clear_sessions
    ):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "hello world")
            run_sync(
                apply_external_content_change(
                    "doc", doc_id,
                    from_cp=6, to_cp=11, new_text="earth",
                    original_text="world",  # matches live slice → applies
                )
            )
            assert session.content == "hello earth"

    def test_surgical_without_original_text_skips_guard(
        self, sync_app, collab_project, _clear_sessions
    ):
        """D2: original_text=None (default) preserves the wholesale behavior —
        no guard, the surgical write proceeds against whatever the live slice is.
        Wholesale callers and legacy tests that don't supply it are unaffected."""
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "hello world")
            run_sync(
                apply_external_content_change(
                    "doc", doc_id,
                    from_cp=6, to_cp=11, new_text="earth",
                    # no original_text → guard skipped
                )
            )
            assert session.content == "hello earth"


def test_surgical_splice_text_helper_is_shared():
    """D5: the cp→byte del/insert primitive lives in ONE place and is imported by
    both the live (events.py) and no-session (tool_api_surface.py) paths."""
    import pycrdt
    from textmatch import surgical_splice_text

    doc = pycrdt.Doc()
    text = doc.get("content", type=pycrdt.Text)
    text += "hello world"
    surgical_splice_text(text, from_cp=6, to_cp=11, new_text="earth")
    assert str(text) == "hello earth"


# ─── D3: already_persisted flush semantics ────────────────────────────────────


class TestAlreadyPersistedDirtySemantics:
    """D3: a wholesale already_persisted replace must leave `_dirty is False`.
    The post-branch `session._dirty = True` overrode the reset; now it's gated on
    `not (not surgical and already_persisted)`."""

    def test_wholesale_already_persisted_leaves_dirty_false(
        self, sync_app, collab_project, _clear_sessions
    ):
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            # WHY: apply_external_content_change publishes the update to the ydoc:{id}
            # backplane, and this same session is subscribed to it. The self-echo is
            # delivered asynchronously on the portal loop and re-applies the update via
            # apply_backplane_update, which unconditionally sets _dirty = True — racing
            # this test's `_dirty is False` assertion (flakes only under a busy loop:
            # full suite / -n). This test targets the events.py D3 branch, not the
            # cross-replica echo, so drop the subscription (on the portal loop that owns
            # it) to isolate the branch's own effect.
            ws.portal.call(session.unsubscribe_backplane)
            _set_session_text(session, "old")
            session._dirty = True  # simulate a prior dirty state
            run_sync(
                apply_external_content_change(
                    "doc", doc_id, "new content", already_persisted=True,
                )
            )
            assert session.content == "new content"
            assert session._dirty is False  # D3 regression

    def test_surgical_edit_is_always_dirty(
        self, sync_app, collab_project, _clear_sessions
    ):
        """D3: surgical edits are real mutations → always dirty, even if a caller
        passes already_persisted=True (a transcription import is wholesale, not
        surgical; the flag must not silently clear dirty on a surgical edit)."""
        from collab.events import apply_external_content_change
        from collab.registry import _session_key, _sessions

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            # WHY: isolate the events.py D3 branch's own _dirty effect from two
            # background interferers that race this `is True` assertion under a busy
            # loop (full suite / -n), mirroring the sibling test above:
            #  - the ydoc:{id} backplane self-echo (apply publishes the update; this
            #    same session is subscribed and re-applies it async via
            #    apply_backplane_update), and
            #  - the periodic flush loop, whose flush_to_db resets _dirty = False
            #    after persisting — the one that flips this True assertion to False.
            # This test targets the surgical branch's dirty semantics, not flush/echo
            # timing, so drop both.
            ws.portal.call(session.unsubscribe_backplane)
            session.stop_periodic_flush()
            _set_session_text(session, "hello world")
            session._dirty = False
            run_sync(
                apply_external_content_change(
                    "doc", doc_id,
                    from_cp=6, to_cp=11, new_text="WORLD",
                    already_persisted=True,  # must NOT clear dirty for surgical
                )
            )
            assert session.content == "hello WORLD"
            assert session._dirty is True


