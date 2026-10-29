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
