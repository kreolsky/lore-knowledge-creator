"""Surface capture + freeze contracts for the tool-ergonomics eval harness.

The eval compares two tool surfaces (mcp-gateway vs tool-api/Pi). Both must reach
the model as ONE normalized shape, and the whole 120-session series must run against
a surface that provably did not move mid-series.
"""
import json

import pytest

from evals.tool_ergonomics import surfaces

# ─── MCP → OpenAI normalization ──────────────────────────────────────────────

def test_mcp_tool_normalizes_to_openai_function_spec():
    """inputSchema becomes function.parameters; name/description survive verbatim."""
    mcp_tool = {
        "name": "edit_document",
        "description": "Edit a document.",
        "inputSchema": {"type": "object", "properties": {"document_id": {"type": "string"}},
                        "required": ["document_id"]},
    }
    spec = surfaces.mcp_tool_to_openai(mcp_tool)
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "edit_document"
    assert spec["function"]["description"] == "Edit a document."
    assert spec["function"]["parameters"] == mcp_tool["inputSchema"]


def test_mcp_tool_without_input_schema_gets_empty_object_schema():
    """A no-arg tool must still carry a valid JSON-Schema object — an absent
    `parameters` makes some providers reject the whole request."""
    spec = surfaces.mcp_tool_to_openai({"name": "init", "description": "Bootstrap."})
    assert spec["function"]["parameters"] == {"type": "object", "properties": {}}


def test_normalize_walks_the_whole_served_list():
    """Derived over the input list, not a hand-copied roster: every served tool
    appears exactly once, in order."""
    served = [{"name": f"t{i}", "description": str(i), "inputSchema": {"type": "object"}}
              for i in range(7)]
    specs = surfaces.normalize_mcp_surface(served)
    assert [s["function"]["name"] for s in specs] == [t["name"] for t in served]


def test_agent_surface_is_already_openai_and_passes_through_unchanged():
    """agent_toolset() emits OpenAI specs already; re-wrapping would double-nest."""
    served = [{"type": "function", "function": {"name": "import_file", "description": "x",
                                                 "parameters": {"type": "object"}}}]
    assert surfaces.normalize_agent_surface(served) == served


def test_agent_surface_rejects_a_non_openai_entry():
    """Crash on a shape change rather than silently shipping a malformed surface."""
    with pytest.raises(ValueError, match="not an OpenAI function spec"):
        surfaces.normalize_agent_surface([{"name": "import_file"}])


# ─── Freeze / thaw ───────────────────────────────────────────────────────────

def test_freeze_roundtrips_specs_and_records_the_commit(tmp_path):
    path = tmp_path / "surface.json"
    specs = [{"type": "function", "function": {"name": "a", "parameters": {"type": "object"}}}]
    surfaces.freeze({"mcp": specs}, commit="deadbeef", path=path)
    snap = surfaces.load_frozen(path, expect_commit="deadbeef")
    assert snap.commit == "deadbeef"
    assert snap.arms["mcp"] == specs


def test_load_frozen_refuses_a_snapshot_from_a_different_commit(tmp_path):
    """The series is comparable only while the surface is frozen. A snapshot taken
    at another commit is not a warning — it invalidates every run that used it."""
    path = tmp_path / "surface.json"
    surfaces.freeze({"mcp": []}, commit="aaaa", path=path)
    with pytest.raises(surfaces.StaleSurface, match="aaaa"):
        surfaces.load_frozen(path, expect_commit="bbbb")


def test_load_frozen_without_expect_commit_still_returns_the_recorded_commit(tmp_path):
    """Reading a snapshot for reporting must not require knowing its commit."""
    path = tmp_path / "surface.json"
    surfaces.freeze({"mcp": []}, commit="aaaa", path=path)
    assert surfaces.load_frozen(path).commit == "aaaa"


def test_freeze_records_the_flags_that_gate_the_served_surface(tmp_path):
    """Both arms are env-gated (SANDBOX_ENABLED / COMFYUI_ENABLED /
    MCP_RUN_EXTRACTOR). A snapshot that does not name them does not identify the
    surface it captured — the same commit serves different tool sets on two hosts."""
    path = tmp_path / "surface.json"
    flags = {"SANDBOX_ENABLED": False, "COMFYUI_ENABLED": True}
    surfaces.freeze({"pi": []}, commit="aaaa", path=path, flags=flags)
    assert surfaces.load_frozen(path).flags == flags


def test_load_frozen_refuses_a_snapshot_captured_under_different_flags(tmp_path):
    """Same failure class as a commit mismatch, and invisible without this check:
    the tool COUNT would differ with no error anywhere."""
    path = tmp_path / "surface.json"
    surfaces.freeze({"pi": []}, commit="aaaa", path=path,
                    flags={"SANDBOX_ENABLED": False})
    with pytest.raises(surfaces.StaleSurface, match="SANDBOX_ENABLED"):
        surfaces.load_frozen(path, expect_commit="aaaa",
                             expect_flags={"SANDBOX_ENABLED": True})


def test_frozen_snapshot_is_human_readable_json(tmp_path):
    """The snapshot is evidence attached to a write-up — it must diff and read."""
    path = tmp_path / "surface.json"
    surfaces.freeze({"mcp": []}, commit="aaaa", path=path)
    assert json.loads(path.read_text())["commit"] == "aaaa"


# ─── Token measurement (the fix-acceptance instrument) ───────────────────────

def test_surface_tokens_counts_the_served_surface_not_a_constant():
    """A fix is accepted against a before/after token measurement, so the
    measurement must move when the surface does."""
    small = [{"type": "function", "function": {"name": "a", "description": "x",
                                               "parameters": {"type": "object"}}}]
    big = small + [{"type": "function", "function": {"name": "b", "description": "y" * 400,
                                                     "parameters": {"type": "object"}}}]
    assert surfaces.surface_tokens(big) > surfaces.surface_tokens(small)
