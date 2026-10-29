"""Batch edit_document — one atomic call carries N pointwise edits.

Plan "quizzical-mixing-marble": apply_edits_to_document resolves + applies an
ordered list of {old_string, new_string} edits in one call (all-or-nothing, one
pre-edit checkpoint, one Y.Doc publish). Re-sending an already-applied edit is
dropped and reported (idempotent skip), NOT silently swallowed — a wrong
old_string whose new_string does not exist is surfaced as an enriched 409.
"""

import asyncio

import pytest
from helpers import join_collab_ws, project_collab_url

# ─── edit_primitives.already_applied (the idempotent-skip probe, audit F1) ────


class TestAlreadyApplied:
    """already_applied(content, old_string, new_string) judges new_string presence
    in the SAME folded projection resolve_edit_range uses for old_string."""

    def test_new_present_once_returns_applied_with_cp(self):
        from agent.edit_primitives import already_applied

        # "hello" present exactly once at code-point 0.
        assert already_applied("hello world", "goodbye", "hello") == ("applied", 0)

    def test_new_present_once_returns_cp_mid_string(self):
        from agent.edit_primitives import already_applied

        # "world" present once at code-point 6.
        assert already_applied("hello world", "x", "world") == ("applied", 6)

    def test_new_absent_returns_absent(self):
        from agent.edit_primitives import already_applied

        assert already_applied("hello world", "x", "zzz") == ("absent", None)

    def test_new_present_twice_returns_ambiguous(self):
        from agent.edit_primitives import already_applied

        assert already_applied("foo bar foo", "x", "foo") == ("ambiguous", None)

    def test_empty_new_string_returns_absent(self):
        from agent.edit_primitives import already_applied

        # A prior deletion is not locatable — surface the miss, never skip.
        assert already_applied("hello world", "hello", "") == ("absent", None)

    def test_fold_consistency_new_with_nbsp_folds_to_space(self):
        """old_string gone; new_string carries a NBSP that folds to a plain space,
        present exactly once after the SAME fold the resolver uses."""
        from agent.edit_primitives import already_applied

        content = "price 100 rub"  # plain space
        result = already_applied(content, "cost 50", "price\u00a0100")  # NBSP
        assert result == ("applied", 0)

    def test_fold_consistency_trailing_spaces_in_content_dropped(self):
        """The fold drops trailing spaces before newlines on the content side; the
        new_string (clean) matches in the folded projection."""
        from agent.edit_primitives import already_applied

        content = "intro\n\noutro"  # prior edit already collapsed the separator
        result = already_applied(content, "intro\n---\n\noutro", "intro\n\noutro")
        assert result == ("applied", 0)


# ─── apply_edits_to_document orchestration (mocked route_document_edits) ──────


def _patch_executor_deps(monkeypatch, *, live_content, route_capture=None):
    """Mock apply_edits_to_document's external deps (fetch_one / access / resolve /
    checkpoint / presence). `route_capture` (if given) installs a spy as
    route_document_edits."""
    from agent import collab_writes as cw

    async def fake_fetch_one(_tbl, _id):
        return {"project_id": "p-1", "content": live_content}

    async def fake_access(_doc_id, _user):
        return "full"

    async def fake_resolve_live_doc_state(_doc_id):
        return live_content, "{}"

    async def fake_checkpoint(**kwargs):
        fake_checkpoint.called = True
        return {"checkpoint_id": "cp-1"}

    fake_checkpoint.called = False

    async def fake_presence(_doc_id, _uid):
        return None

    # M7: fetch+RBAC+scope gate moved to scope.gate_mutation_target (lazy-resolves
    # fetch_one / get_document_access from their source modules) — patch those.
    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)
    if route_capture is not None:
        original_captured = route_capture

        async def fake_route_edits(*, doc_id, edits, project_id):
            original_captured["edits"] = edits
            original_captured["project_id"] = project_id
            return True

        monkeypatch.setattr("agent.doc_state.route_document_edits", fake_route_edits)
    return fake_checkpoint


@pytest.mark.asyncio
async def test_batch_applies_two_edits_one_checkpoint_descending(monkeypatch):
    """Two non-overlapping edits apply in ONE call; route_document_edits receives
    them in DESCENDING from_cp order (earlier offsets stay valid under splicing)."""
    from agent import tool_api_surface as tus

    capture: dict = {}
    cp_spy = _patch_executor_deps(monkeypatch, live_content="alpha beta gamma", route_capture=capture)

    result = await tus.apply_edits_to_document(
        doc_id="doc-A",
        edits=[{"old_string": "alpha", "new_string": "ALPHA"},
               {"old_string": "gamma", "new_string": "GAMMA"}],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert result["applied"] == 2
    assert result["skipped"] == []
    assert cp_spy.called, "expected exactly one pre-edit checkpoint"
    # gamma (cp 11..16) comes before alpha (cp 0..5) in descending order.
    from_cps = [e["from_cp"] for e in capture["edits"]]
    assert from_cps == sorted(from_cps, reverse=True)


@pytest.mark.asyncio
async def test_batch_all_skipped_returns_noop_no_checkpoint(monkeypatch):
    """Re-sending an already-applied edit: old gone + new present once → skipped
    with {index, at_cp, reason}; NO checkpoint row created (applied == 0)."""
    from agent import tool_api_surface as tus

    # Prior edit already replaced "quick" → "QUICK".
    cp_spy = _patch_executor_deps(monkeypatch, live_content="the QUICK fox")

    result = await tus.apply_edits_to_document(
        doc_id="doc-A",
        edits=[{"old_string": "quick", "new_string": "QUICK"}],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert result.get("noop") is True
    assert result["applied"] == 0
    assert result["skipped"] == [{"index": 0, "at_cp": 4, "reason": "already_applied"}]
    assert cp_spy.called is False, "no checkpoint when applied == 0"


@pytest.mark.asyncio
async def test_batch_partial_skip_partial_apply(monkeypatch):
    """A mix: one edit already applied (skipped), one still pending (applied). The
    checkpoint IS created (applied >= 1), and skipped is reported."""
    from agent import tool_api_surface as tus

    # "QUICK" already applied; "fox" still pending.
    capture: dict = {}
    cp_spy = _patch_executor_deps(monkeypatch, live_content="the QUICK fox", route_capture=capture)

    result = await tus.apply_edits_to_document(
        doc_id="doc-A",
        edits=[{"old_string": "quick", "new_string": "QUICK"},   # already applied
               {"old_string": "fox", "new_string": "CAT"}],       # pending
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert result["applied"] == 1
    assert result["skipped"] == [{"index": 0, "at_cp": 4, "reason": "already_applied"}]
    assert cp_spy.called, "checkpoint created because applied >= 1"
    # Only the pending edit reaches route_document_edits.
    assert len(capture["edits"]) == 1
    assert capture["edits"][0]["new_text"] == "CAT"


@pytest.mark.asyncio
async def test_batch_edit_unwraps_placeholder_bracket_link_destinations(monkeypatch):
    """A `(<id>)` link the model copied from a prompt placeholder reaches the
    convergence path unwrapped — the batch edit executor is one of the three
    agent write sites for the unwrap (a non-empty append converges onto it as a
    synthesized edit)."""
    from agent import tool_api_surface as tus

    capture: dict = {}
    _patch_executor_deps(monkeypatch, live_content="alpha beta gamma", route_capture=capture)

    result = await tus.apply_edits_to_document(
        doc_id="doc-A",
        edits=[{"old_string": "alpha",
                "new_string": "alpha [text](<abc-def>) and ![a|800x600](<ref:xyz_1>)"}],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    stored = capture["edits"][0]["new_text"]
    assert stored == "alpha [text](abc-def) and ![a|800x600](ref:xyz_1)"


@pytest.mark.asyncio
async def test_batch_wrong_old_new_absent_enriched_409_with_index(monkeypatch):
    """A failing edit (wrong old_string AND new_string absent) rejects the WHOLE
    batch with an enriched not_found + the failing index; nothing is applied."""
    from agent import tool_api_surface as tus

    capture: dict = {}
    cp_spy = _patch_executor_deps(monkeypatch, live_content="alpha beta gamma", route_capture=capture)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await tus.apply_edits_to_document(
            doc_id="doc-A",
            edits=[{"old_string": "alpha", "new_string": "ALPHA"},
                   {"old_string": "nonexistent", "new_string": "XYZ"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["index"] == 1
    assert "not_found" in detail.get("error", "") or "not_found" in str(detail)
    assert cp_spy.called is False
    assert "edits" not in capture, "nothing should reach route_document_edits"


# ─── route_document_edits: one Y.Doc publish for a multi-edit batch (F5) ──────


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


class TestRouteDocumentEditsSinglePublish:
    """audit F5: route_document_edits applies N surgical splices on ONE loaded
    Y.Doc under ONE _write_lock, then emits exactly ONE publish_doc_update."""


    def test_single_edit_batch_one_publish(
        self, sync_app, collab_project, _clear_sessions, monkeypatch,
    ):
        """A one-edit batch also publishes exactly once (degenerate case)."""
        from collab.registry import _session_key, _sessions

        import ydoc_store

        pid, doc_id, admin_token, *_ = collab_project
        with sync_app.websocket_connect(
            project_collab_url(pid), cookies={"lore_session": admin_token}
        ) as ws:
            join_collab_ws(ws, doc_id)
            ws.receive_text()
            session = _sessions[_session_key("doc", doc_id)]
            _set_session_text(session, "hello world")

            publish_count = {"n": 0}

            async def spy_publish(*a, **kw):
                publish_count["n"] += 1
                return None

            monkeypatch.setattr(ydoc_store, "publish_doc_update", spy_publish)

            from agent.doc_state import route_document_edits

            loop = asyncio.get_event_loop()
            loop.run_until_complete(
                route_document_edits(
                    doc_id=doc_id,
                    edits=[{"from_cp": 6, "to_cp": 11, "new_text": "WORLD",
                            "original_text": "world"}],
                    project_id=None,
                )
            )
            assert session.content == "hello WORLD"
            assert publish_count["n"] == 1
