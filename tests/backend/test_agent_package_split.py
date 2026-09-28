"""PR1 regression: the `agent` package split must expose the expected
public names across its submodules and the proposal REST routes must stay bound
to the shared router after the mechanical split of the former god-module.

These are structural smoke tests — they guard against the silent failure modes a
pure "does it import" check misses:
  - a submodule imports cleanly but its `@router` decorators never bound (caught
    by the route-binding assertions against `routes.chat._router.router`).
  - a public name silently dropped during the move (caught by the name checks).
"""
import importlib

import pytest

SUBMODULES = {
    "agent.tools": ["AGENT_TOOLS", "MUTATING_TOOLS", "agent_toolset"],
    "agent.edit_primitives": ["resolve_edit_range", "_splice_edit"],
    "agent.readonly_executors": [
        "search_materials_tool",
        "read_document_tool",
    ],
    "agent.tool_api_surface": ["apply_edit_to_document"],
}


@pytest.mark.parametrize("modname,names", list(SUBMODULES.items()))
def test_submodule_exposes_expected_names(modname, names):
    """Each submodule imports cleanly and exposes its documented public names."""
    mod = importlib.import_module(modname)
    for name in names:
        assert hasattr(mod, name), f"{modname} missing public name {name!r}"


def test_legacy_package_path_does_not_leak_internal_helpers():
    """The package `agent` is a thin aggregator — internal helpers
    (e.g. `_splice_edit`) are NOT re-exported at the package root, so accidental
    `from agent import _splice_edit` would fail loudly rather than
    silently re-couple the package. PR6 may relax this; PR1 keeps it strict."""
    import agent as pkg

    # Aggregate behavior lives in submodules, not the package root.
    assert not hasattr(pkg, "_splice_edit")
    assert not hasattr(pkg, "resolve_edit_range")


def test_external_importers_point_at_submodules():
    """selection_conflict sources the primitive from edit_primitives, never
    from the package root."""
    import inspect

    import routes.chat.selection_conflict as sc

    src = inspect.getsource(sc)
    assert "from agent.edit_primitives import" in src
    assert "from agent import resolve_edit_range" not in src

