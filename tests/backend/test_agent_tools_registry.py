"""Tool registry contract tests (plan pi-agent-layer-collapse, step 1).

# SYSTEM: agent-tools — the ONE tool registry binding every surface.

The registry (backend/agent_tools/) is the single declaration per agent tool
(name, JSON schema, request model, handler, mutating, surfaces). AGENT_TOOLS /
MUTATING_TOOLS / await agent_toolset() and the Tool-API POST routes are all DERIVED
from it. These tests bind the derivations to the registry so a tool without
registry coverage fails loud (totality), and so no derivation silently drifts
(deep-equal + order pins against the declared entries, never literal lists).
"""

import json

import pytest


def _registry():
    from agent_tools import registry

    return registry


# ─── Entry shape ──────────────────────────────────────────────────────────────


def test_every_entry_declares_the_full_shape():
    from pydantic import BaseModel

    reg = _registry()
    assert reg.REGISTRY, "registry is empty"
    for entry in reg.REGISTRY.values():
        assert entry.name == entry.spec["function"]["name"], entry.name
        assert entry.spec["type"] == "function"
        json.dumps(entry.spec)  # OpenAI wire-serializable
        assert "mutating" in entry.spec, f"{entry.name}: no `mutating` declaration"
        assert isinstance(entry.spec["mutating"], bool)
        assert entry.surfaces, f"{entry.name}: no surfaces"
        if "tool_api" in entry.surfaces:
            # The generated route needs a pydantic body model. mcp-only entries
            # (gateway tools) may consume raw args instead.
            assert isinstance(entry.request_model, type) and issubclass(
                entry.request_model, BaseModel
            ), f"{entry.name}: tool_api entry without a pydantic request_model"


def test_every_mcp_served_mutating_entry_carries_clean_description():
    reg = _registry()
    missing = [
        e.name for e in reg.mcp_entries()
        if e.mutating and e.request_model is not None
        and not (e.mcp_description or "").strip()
    ]
    assert not missing, (
        f"mcp-served mutating tools without a registry mcp_description "
        f"(would leak proposal wording): {missing}"
    )


def test_advertised_equals_routed_equals_registry_mcp_surface():
    """The MCP tools/list surface, dispatch_tool's served set, and the
    registry's mcp entries are ONE set (minus the preview_extractor deployment
    gate when it is off) — the advertised-vs-routed guard is structural over the
    registry."""
    from mcp_gateway.schemas import (
        _preview_extractor_visible,
        _served_mcp_names,
        build_tool_list,
    )

    reg = _registry()
    advertised = {t.name for t in build_tool_list()}
    assert advertised == _served_mcp_names()
    expected = {e.name for e in reg.mcp_entries()}
    if not _preview_extractor_visible():
        expected.discard("preview_extractor")
    assert advertised == expected


def test_every_tool_api_entry_handler_resolves():
    reg = _registry()
    for entry in reg.tool_api_entries():
        handler = reg.resolve_handler(entry)
        assert callable(handler), entry.name


# ─── Derived sets ─────────────────────────────────────────────────────────────


def test_mutating_set_is_derived_from_registry_declarations():
    from agent.tools import MUTATING_TOOLS

    reg = _registry()
    declared = {
        e.name for e in reg.REGISTRY.values()
        if e.spec["mutating"] and "agent" in e.surfaces
    }
    assert declared == set(MUTATING_TOOLS), (
        "MUTATING_TOOLS drifted from the registry declarations"
    )


def test_agent_tools_equals_mcp_surface_of_registry_in_order():
    from agent.tools import AGENT_TOOLS

    reg = _registry()
    # Expected = the registry public surface rendered the Pi way (the {{ROOT}}
    # sentinel filled with the unscoped constant — AGENT_TOOLS serves text, and
    # an unfilled sentinel would reach consumers as a literal). The fill is the
    # registry's own (fill_spec_root), so this still binds membership + order +
    # every non-sentinel byte to the declaration.
    from agent_tools.registry import ROOT_UNSCOPED_FILL, fill_spec_root

    public_specs = [
        fill_spec_root(e.spec, ROOT_UNSCOPED_FILL) for e in reg.REGISTRY.values()
        if "mcp" in e.surfaces and "agent" in e.surfaces
    ]
    assert AGENT_TOOLS == public_specs, (
        "AGENT_TOOLS drifted from the registry public surface (deep-equal, order incl.)"
    )
    assert [t["function"]["name"] for t in AGENT_TOOLS] == [
        "search_materials", "read_document", "get_project_structure",
        "create_document", "edit_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
    ]


async def test_agent_toolset_full_order_with_gates_on(monkeypatch):
    from agent import tools
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=True, comfy=True)
    names = [t["function"]["name"] for t in await tools.agent_toolset()]
    assert names == [
        "search_materials", "read_document", "get_project_structure",
        "create_document", "edit_document", "edit_table_cell",
        "create_table", "add_table_rows", "add_table_column",
        "append_to_document", "move_document", "rename_document",
        "sandbox_bash", "sandbox_fetch_reference", "sandbox_fetch_skill",
        "sandbox_run_status",
        "generate_image",
        "reprocess_reference", "import_file",
        "consolidate_memory", "next_reference",
        "get_memory_facts", "get_fact_history",
        "apply_memory_verdicts", "reopen_consolidation",
        "save_skill",
    ]


async def test_agent_toolset_gates_drop_exactly_the_gated_tools(monkeypatch):
    from agent import tools
    from helpers import pin_tool_gates

    pin_tool_gates(monkeypatch, sandbox=False, comfy=False)
    names = {t["function"]["name"] for t in await tools.agent_toolset()}
    assert not ({"sandbox_bash", "sandbox_fetch_reference", "sandbox_run_status",
                 "generate_image"} & names)
    # Everything else is ungated and stays.
    assert {"search_materials", "import_file",
            "consolidate_memory"} <= names


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


# ─── Totality: registry ↔ served Tool-API routes ──────────────────────────────


def test_registry_totality_against_served_tool_api_routes():
    from main import app

    reg = _registry()
    route_names = {
        r.path.removeprefix("/api/tool/")
        for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set())
        and "{" not in r.path
    }
    entry_names = {e.name for e in reg.tool_api_entries()}
    assert entry_names <= route_names, (
        f"registry entries with no served route: {sorted(entry_names - route_names)}"
    )
    assert route_names <= entry_names, (
        f"served tool routes with no registry entry: {sorted(route_names - entry_names)}"
    )


def test_generated_routes_bind_the_registry_handler_and_keep_operation_ids():
    """The generated route's endpoint must BE the registry handler (telemetry parity
    with the pre-registry decorator stack) and the OpenAPI operation ids must keep
    the tool_<name>_api_tool_<name>_post shape the tracked openapi.json publishes
    (openapi-export system)."""
    from main import app

    reg = _registry()
    by_path = {
        r.path: r for r in app.routes
        if getattr(r, "path", "").startswith("/api/tool/")
        and "POST" in getattr(r, "methods", set())
        and "{" not in r.path
    }
    for entry in reg.tool_api_entries():
        route = by_path[f"/api/tool/{entry.name}"]
        handler = reg.resolve_handler(entry)
        assert route.endpoint.__name__ == handler.__name__, entry.name
        assert (route.endpoint.__doc__ or "") == (handler.__doc__ or ""), (
            f"{entry.name}: generated route lost the handler docstring "
            "(OpenAPI description regression)"
        )
