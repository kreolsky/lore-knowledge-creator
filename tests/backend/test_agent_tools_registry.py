"""Tool registry contract tests (plan pi-agent-layer-collapse, step 1).

# SYSTEM: agent-tools — the ONE tool registry binding every surface.

The registry (backend/agent_tools/) is the single declaration per agent tool
(name, JSON schema, request model, handler, mutating, surfaces). AGENT_TOOLS /
MUTATING_TOOLS / await agent_toolset() and the Tool-API POST routes are all DERIVED
from it. These tests bind the derivations to the registry so a tool without
registry coverage fails loud (totality), and so no derivation silently drifts
(deep-equal + order pins against the declared entries, never literal lists).
"""


import pytest


def _registry():
    from agent_tools import registry

    return registry


# ─── Derived sets ─────────────────────────────────────────────────────────────


async def test_use_skill_is_gone_one_skill_loader_only():
    """The model sees exactly ONE skill loader — dsh's `skill` tool. use_skill
    is DELETED (plan collapse-the-editor-harness-layer step 3): bodies ride the
    payload's raw skills wire, so the lore-skills provider has no Tool-API body
    fetch to call and there is no second loader to keep off the turn payload.
    Derived from the registry + the live app routes, never a literal list."""
    from agent.tools import agent_toolset

    from main import app

    reg = _registry()
    with pytest.raises(KeyError):
        reg.by_name("use_skill")
    names = {t["function"]["name"] for t in await agent_toolset()}
    assert "use_skill" not in names
    route_names = {
        r.path.removeprefix("/api/tool/")
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set())
        and "{" not in r.path
    }
    assert "use_skill" not in route_names
