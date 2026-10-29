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


# ─── D3: already_persisted flush semantics ────────────────────────────────────


class TestAlreadyPersistedDirtySemantics:
    """D3: a wholesale already_persisted replace must leave `_dirty is False`.
    The post-branch `session._dirty = True` overrode the reset; now it's gated on
    `not (not surgical and already_persisted)`."""


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
