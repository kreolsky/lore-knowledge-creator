"""Agent tool surface — the DERIVED view over the agent_tools registry.

# Agent tool surface, the permission boundary (see SYSTEM: chat-agent-mode, agent/__init__.py).

# ARCH: the tool surface IS the permission
# boundary. dsh is the ONLY AI line — every AI chat gets the full agent toolset
# (the Ask zero-tools line was deleted; there is no second surface).

# ARCH: the ONE declaration per tool lives in
# backend/agent_tools/ (registry + entries — name, schema, request model,
# handler, `mutating`, surfaces). This module DERIVES the agent-facing bindings:
#   - AGENT_TOOLS    — the "mcp" registry subset = the public surface (the
#                      gateway's build_tool_list derives from it);
#   - MUTATING_TOOLS — every declared mutating tool, STATIC (the dsh driver
#                      serializes unconditionally, independent of env gates);
#   - agent_toolset()— the "agent" subset with the env gates applied.
# It stays a leaf in the LOGIC import DAG: it imports no sibling agent logic
# submodule (completions / driver.client / proposals / readonly_executors all
# import FROM here, never the reverse).
"""
# config is not an agent submodule, so this does not violate the leaf rule above.
# The gate FOLDS (SANDBOX_ENABLED / COMFYUI_ENABLED) are NOT
# import-time reads anymore: _gate_on re-derives each from its BASE keys through
# settings at call time (row → env → default), so a settings row on a base key
# reaches the served surface on the next turn. Tests pin the gates by patching
# the config base attrs (SANDBOX_SSH_HOST/KEY_B64, COMFYUI_URL).
import settings
from agent_tools.registry import ROOT_UNSCOPED_FILL, _is_region_capable
from agent_tools.registry import entries as _registry_entries
from agent_tools.registry import fill_spec_root as _fill_spec_root
from agent_tools.registry import holdable_names as _registry_holdable_names
from agent_tools.registry import mutating_names as _registry_mutating_names


async def _gate_on(gate: str | None) -> bool:
    """Read one agent env gate at CALL time (unset base config = the tool is
    simply not served — a valid deploy, not an error). Each gate is re-derived
    from its BASE keys (the config *_ENABLED constants are import-time folds a
    settings row could never reach)."""
    if gate == "sandbox":
        vals = await settings.get_all(["SANDBOX_SSH_HOST", "SANDBOX_SSH_KEY_B64"])
        return bool(vals["SANDBOX_SSH_HOST"] and vals["SANDBOX_SSH_KEY_B64"])
    if gate == "comfyui":
        return bool(await settings.get("COMFYUI_URL"))
    return True


def _agent_spec(entry) -> dict:
    """One entry's wire spec RENDERED for the agent surface: the {{ROOT}} sentinel
    (registry.fill_spec_root, copy-on-need) filled with the UNSCOPED constant.

    # ARCH: a CONSTANT fill, never per-key — the agent path runs on internal keys
    # (empty scope_root), so 'the project root' is TRUE on this surface and one
    # substitution at derivation is the whole render. The param-level sentinel
    # sites are SHARED with MCP (both paths serve the same spec dicts verbatim),
    # so this derivation is what keeps a literal {{ROOT}} out of the in-app chat.
    """
    return _fill_spec_root(entry.spec, ROOT_UNSCOPED_FILL)


async def agent_toolset() -> list[dict]:
    """Return the tool surface for an AI chat turn.

    The registry's declaration order (filtered, never re-sorted): the public
    base surface first, then the env-gated internal tools (sandbox / ComfyUI —
    internal services an external MCP client cannot reach), then the
    always-served agent-only tools (reprocess, import, memory loop).

    # ARCH: this — NOT AGENT_TOOLS — is where the sandbox console is served.
    # Its only consumer is the agent path (routes/chat/completions.py), while
    # mcp-gateway, which lets ANY external harness speak the agent surface,
    # derives its list FROM AGENT_TOOLS via build_tool_list(). A tool declared
    # without the "mcp" surface never ships to third-party MCP clients.
    """
    return [
        _agent_spec(e) for e in _registry_entries()
        if "agent" in e.surfaces and await _gate_on(e.env_gate)
    ]


# The public surface = the registry's mcp∩agent subset, in declaration order (the
# gateway tools are mcp-ONLY — they never join the agent surface). Rendered with
# the same unscoped constant fill as agent_toolset (see _agent_spec).
AGENT_TOOLS: list[dict] = [
    _agent_spec(e) for e in _registry_entries()
    if "mcp" in e.surfaces and "agent" in e.surfaces
]


# Derived from the per-entry `mutating` declarations on the registry specs
# (single source — do NOT hand-list members here; edit the tool entry instead).
# STATIC over every declared tool regardless of env gates or served surface:
# the dsh driver serializes this set unconditionally (driver.client._build_turn_payload).
# WHY: every entry declares `mutating` explicitly — registry.register rejects a
# spec without it, so a new tool that omits the field fails at import (loud),
# and the totality test
# (test_agent_tools_registry.test_mutating_set_is_derived_from_registry_declarations)
# binds this set to the declarations — a derivation bug fails loud, never
# silently ships a tool outside the mutating set.
MUTATING_TOOLS: frozenset[str] = _registry_mutating_names()

# The subset of MUTATING_TOOLS the mid-turn gate can actually hold — mutating
# tools whose request model carries the `apply` field. Serialized to the dsh
# driver so it only announces a verdict ask for tools that really hold (a
# card for sandbox_bash/import_file would claim a call is held that already
# ran). Derived, never hand-listed — see the totality test on MUTATING_TOOLS.
HOLDABLE_TOOLS: frozenset[str] = _registry_holdable_names()


# The agent tools whose request model carries `region` — the only tools a pinned
# session can confine to its fragment. DERIVED with the pin gate's own predicate
# (agent_tools.registry._is_region_capable), never hand-listed: a new
# region-capable tool joins by declaring the field, not by editing a set here.
# Serialized to the dsh driver as `region_tools` (the mutating_tools contract
# shape) so the driver never re-hardcodes {edit_document, append_to_document} —
# the mirrored-literal drift this serialization closes.
REGION_TOOLS: frozenset[str] = frozenset(
    e.name for e in _registry_entries()
    if "agent" in e.surfaces and _is_region_capable(e)
)
