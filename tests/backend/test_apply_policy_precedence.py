"""PR4 R2: a single `resolve_apply_mode` replaces the three drifting resolvers
(driver.client.resolve_agent_apply_mode, tool_api._wants_auto/_can_auto_apply,
agent_config.system_doc_edit_policy). The precedence is a CHOSEN truth table (plan
§PR4 R2 / S2) — unification necessarily changes some edge cases, so the table test
pins the chosen behavior, not the legacy one.

Step 4 (proposal cluster deletion) collapsed the table further: the `access_level`
cell is gone (RBAC-at-dispatch 403s a non-full caller before the resolver runs) and
the `has_region` cell is gone (containment is enforced on the direct apply path, not
by forcing confirm). The resolver is pure (no I/O).
"""
import pytest

CASES = [
    # (is_system, ui_preference, expected_mode)
    # Row 1: a system doc NEVER auto-applies, regardless of any other input.
    (True,  "auto",    "confirm"),
    (True,  "confirm", "confirm"),
    # Row 2: the ONLY auto cell — user opted in, non-system target.
    (False, "auto",    "auto"),
    # Row 3: user chose confirm.
    (False, "confirm", "confirm"),
]


@pytest.mark.parametrize(
    "is_system,ui,expected", CASES,
)
def test_resolve_apply_mode_precedence(is_system, ui, expected):
    """First-match-wins precedence: system_doc → auto → confirm."""
    from agent.apply_policy import resolve_apply_mode

    decision = resolve_apply_mode(is_system=is_system, ui_preference=ui)
    assert decision.mode == expected


def test_decision_carries_a_reason_label():
    """The decision exposes a short reason for tracing (not just the mode)."""
    from agent.apply_policy import resolve_apply_mode

    assert resolve_apply_mode(
        is_system=True, ui_preference="auto",
    ).reason == "system_doc"
    assert resolve_apply_mode(
        is_system=False, ui_preference="auto",
    ).reason == "opted_in"
    assert resolve_apply_mode(
        is_system=False, ui_preference="confirm",
    ).reason == "ui_confirm"


def test_only_one_auto_cell_in_full_table():
    """Across the whole Cartesian product, exactly one combination yields auto —
    the plan's invariant that auto requires opted-in + non-system."""
    from agent.apply_policy import resolve_apply_mode

    auto_count = 0
    for is_system in (True, False):
        for ui in ("auto", "confirm"):
            if resolve_apply_mode(
                is_system=is_system, ui_preference=ui,
            ).mode == "auto":
                auto_count += 1
    assert auto_count == 1


async def test_verdict_approved_bypasses_the_resolver_cells():
    """The driver's approval marker (X-Agent-Verdict — dsh's user-approval
    service already asked and the user allowed) must NOT be downgraded back to
    a second confirmation by the handler's own apply-mode re-resolution — on a
    system doc too: the marker reaches ctx only on a driver-attested request
    (agent.context.driver_attested), so it is the user's approval of the call."""
    from routes.tool_api._common import _resolve_apply_or_force

    for is_system in (False, True):
        decision = await _resolve_apply_or_force(
            {"verdict": "allowed-once"},
            ui_preference="confirm", is_system=is_system,
        )
        assert decision.mode == "auto"
        assert decision.reason == "verdict_approved"
    # the ordinary path (no verdict) still confirms on system docs
    decision2 = await _resolve_apply_or_force(
        {}, ui_preference="confirm", is_system=True,
    )
    assert decision2.mode == "confirm"


def test_legacy_resolvers_are_gone():
    """The three drifting resolvers are deleted, not just shadowed."""
    import agent_config
    import driver.client
    import routes.tool_api as tool_api

    assert not hasattr(driver.client, "resolve_agent_apply_mode"), (
        "driver.client.resolve_agent_apply_mode must move to apply_policy"
    )
    assert not hasattr(tool_api, "_wants_auto"), "tool_api._wants_auto deleted"
    assert not hasattr(tool_api, "_can_auto_apply"), "tool_api._can_auto_apply deleted"
    assert "system_doc_edit_policy" not in agent_config.__all__, (
        "agent_config.system_doc_edit_policy folded into apply_policy"
    )


# ─── Direct-apply survival (moved from the deleted test_proposal_deletion_survival.py) ──


@pytest.mark.asyncio
async def test_direct_apply_still_re_resolves_against_live_content(monkeypatch):
    """The auto/direct apply path re-resolves old_string against LIVE content — a
    stale old_string still 409s (the proposal apply path that once carried this is
    gone, but the re-resolution belongs to the executors and survives)."""
    from agent import collab_writes as cw
    from agent import tool_api_surface as tus
    from fastapi import HTTPException

    async def fake_fetch_one(_tbl, _id):
        return {"project_id": "p-1", "content": "live content here"}

    async def fake_access(_doc_id, _user):
        return "full"

    async def fake_resolve(_doc_id):
        return "live content here", "{}"

    async def fake_checkpoint(**kwargs):
        return {"checkpoint_id": "cp-1"}

    async def fake_presence(_doc_id, _uid):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    with pytest.raises(HTTPException) as exc:
        await tus.apply_edits_to_document(
            doc_id="doc-A",
            edits=[{"old_string": "stale text", "new_string": "replacement"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_pinned_region_edit_escaping_fragment_is_region_locked(monkeypatch):
    """A pinned-region edit whose resolved range escapes the fragment is rejected as
    `region_locked` on the DIRECT apply path (the deleted proposal path's
    region_out_of_scope status is gone; the wire signal is region_locked)."""
    from agent import collab_writes as cw
    from agent import tool_api_surface as tus
    from fastapi import HTTPException

    from models import RegionRef

    live = "alpha beta gamma"

    async def fake_fetch_one(_tbl, _id):
        return {"project_id": "p-1", "content": live}

    async def fake_access(_doc_id, _user):
        return "full"

    async def fake_resolve(_doc_id):
        return live, "{}"

    async def fake_checkpoint(**kwargs):
        return {"checkpoint_id": "cp-1"}

    async def fake_presence(_doc_id, _uid):
        return None

    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)
    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    # The region pins only "alpha" (cp 0..5); the edit targets "gamma" (cp 11..16).
    with pytest.raises(HTTPException) as exc:
        await tus.apply_edits_to_document(
            doc_id="doc-A",
            edits=[{"old_string": "gamma", "new_string": "GAMMA"}],
            project_id="p-1", user={"user_id": "u-1"},
            region=RegionRef(doc_id="doc-A", from_cp=0, to_cp=5),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "region_locked"
    assert "pinned" in exc.value.detail["detail"]
