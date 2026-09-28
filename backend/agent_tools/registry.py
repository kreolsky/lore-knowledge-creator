"""The tool registry — one ToolEntry per agent tool, derived everywhere else.

# SYSTEM: agent-tools (see package __init__) — this module holds the table.

# ARCH: the registry is a LEAF (stdlib only). Handlers are referenced by dotted
# path and resolved lazily (`resolve_handler`) so declaring a tool never drags
# the route graph into import; the surfaces a tool serves are DATA here, not
# code scattered across four modules (the four copies this registry replaced:
# the AGENT_TOOLS assembly, MUTATING_TOOLS, the hand-decorated Tool-API routes,
# and the MCP routing sets).

# INVARIANT: `mutating` is declared on the spec itself (spec["mutating"], a
# bool) and read with no default — a tool that omits it fails at registration.
# Why: the derivation `mutating_names()` is the single source for
# agent.tools.MUTATING_TOOLS; a silent default would let a new tool
# fall out of the mutating set (skipping sequential execution + `apply`
# injection in the dsh driver — a lost-update risk) with nothing failing.
"""
from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass

#: The surfaces a tool can be served on. "agent" = the in-chat agent toolset;
#: "tool_api" = an HTTP POST /api/tool/{name} route; "mcp" = the external
#: MCP tools/list surface (a strict subset of agent — everything on mcp is on agent).
SURFACES = frozenset({"agent", "tool_api", "mcp"})

#: The agent env gates (read at call time by agent.tools._gate_on —
#: the gate VALUES stay bound there because tests monkeypatch that module's
#: attrs, the historic binding surface).
ENV_GATES = frozenset({"sandbox", "comfyui"})

# ─── The {{ROOT}} sentinel — a served text names the caller's actual root ─────
#
# ARCH: "project root" in a served tool text is a LIE for a subtree-scoped agent
# key — the executor resolves a null/omitted parent to the KEY's own root
# (scope.resolve_scoped_parent), so the text a model reads when it chooses
# parent_id:null must name that root. A {{ROOT}} sentinel is filled at SERVE
# time: per key on MCP (mcp_gateway.server.list_tools fetches the scope root's
# title), with the unscoped constant on the agent/Tool-API surfaces (internal keys
# carry an empty scope_root — "the project root" is TRUE there). The mechanism is
# the one bootstrap already uses for {{READ_TOOLS}}: per-call, never frozen at
# import.
ROOT_SENTINEL = "{{ROOT}}"

#: The unscoped fill — also the constant the agent derivation applies (see ARCH
#: above). Kept here, next to the sentinel, so the two surfaces cannot drift on
#: the unscoped wording.
ROOT_UNSCOPED_FILL = "the project root"


def root_fill(scope_root: str, root_title: str) -> str:
    """The {{ROOT}} fill for a caller's actual root.

    Unscoped (empty scope_root) → the constant. Scoped → the root's title + id,
    the two things a model needs to NAME its root in a call. An empty title (a
    deleted scope root) renders empty — parity with init's capabilities.scope
    today; consistency with init beats a new error path.
    """
    if not scope_root:
        return ROOT_UNSCOPED_FILL
    return f'the "{root_title}" subtree root ({scope_root})'


def fill_root(text: str, fill: str) -> str:
    """Fill the sentinel in one description string. (str.replace returns the
    SAME object when the sentinel is absent — the sentinel-free pass is
    byte-identical by construction.)"""
    return text.replace(ROOT_SENTINEL, fill)


def _props_carry_sentinel(props: dict) -> bool:
    return any(
        isinstance(p, dict) and ROOT_SENTINEL in (p.get("description") or "")
        for p in props.values()
    )


def fill_properties_root(schema: dict, fill: str) -> dict:
    """Fill the sentinel in a parameters dict's properties.*.description — the
    PARAM-level half of the render (a model reads exactly these strings when it
    chooses parent_id:null).

    Copy-on-need: the SAME dict object when no property carries the sentinel.
    Why: the registry spec dicts are SHARED across the agent/Tool-API/MCP surfaces
    (schemas passes spec["parameters"] straight into Tool.inputSchema) — filling
    in place would write one key's root into every other surface's text; and a
    sentinel-free pass must stay byte-identical, which only object reuse gives
    for free.
    """
    props = schema.get("properties")
    if not isinstance(props, dict) or not _props_carry_sentinel(props):
        return schema
    new_props = {
        k: (
            {**p, "description": fill_root(p["description"], fill)}
            if isinstance(p, dict) and ROOT_SENTINEL in (p.get("description") or "")
            else p
        )
        for k, p in props.items()
    }
    return {**schema, "properties": new_props}


def fill_spec_root(spec: dict, fill: str) -> dict:
    """Fill the sentinel in a full wire spec — function.description AND its
    parameters' property descriptions. Copy-on-need (see fill_properties_root):
    the SAME spec object when neither level carries the sentinel."""
    fn = spec.get("function") or {}
    if not isinstance(fn, dict):
        return spec
    desc = fn.get("description") or ""
    has_desc = ROOT_SENTINEL in desc
    params = fn.get("parameters")
    new_params = (
        fill_properties_root(params, fill) if isinstance(params, dict) else params
    )
    if not has_desc and new_params is params:
        return spec
    new_fn: dict = {**fn}
    if has_desc:
        new_fn["description"] = fill_root(desc, fill)
    if new_params is not params:
        new_fn["parameters"] = new_params
    return {**spec, "function": new_fn}


@dataclass(frozen=True)
class ToolEntry:
    """One agent tool, declared once (assembled in agent_tools.entries).

    spec          — the OpenAI function-calling wire spec (pure data from
                    agent_tools.specs; carries the `mutating` bool).
    request_model — the pydantic body model for the Tool-API route.
    handler_path  — "dotted.module:callable" of the handler (resolved lazily
                    and cached; the handler keeps its telemetry decorator).
    surfaces      — which surfaces serve it (subset of SURFACES).
    env_gate       — optional env gate ("sandbox" | "comfyui") that
                    drops the tool from the agent toolset when off. None = always
                    served. The gate never affects the Tool-API route (the
                    handler checks its own config) or MUTATING_TOOLS (static).
    """

    spec: dict
    request_model: type | None
    handler_path: str
    surfaces: frozenset[str]
    env_gate: str | None = None
    #: MCP-only description override. The shared spec description describes the
    #: in-app proposal/confirmation flow (real on the agent/Tool-API path); that
    #: flow does NOT exist over MCP, so a mutating mcp tool REPLACES its text
    #: here with clean proposal-free wording. None = the spec text serves as-is.
    mcp_description: str | None = None

    @property
    def name(self) -> str:
        return self.spec["function"]["name"]

    @property
    def mutating(self) -> bool:
        return self.spec["mutating"]


#: name → ToolEntry, in DECLARATION order. The declaration order IS the agent
#: serving order (agent_toolset filters, never re-sorts); the "mcp" subset of
#: it is AGENT_TOOLS. Do not reorder casually: the agent tool list and the MCP
#: tools/list are emitted in this order.
REGISTRY: dict[str, ToolEntry] = {}

#: Resolved handler cache (handler_path → callable).
_HANDLERS: dict[str, Callable] = {}


def register(entry: ToolEntry) -> ToolEntry:
    """Register one tool. Fails loud on a duplicate name or a missing
    `mutating` declaration (the INVARIANT above)."""
    if "mutating" not in entry.spec:
        raise ValueError(f"tool {entry.name}: spec lacks the `mutating` declaration")
    if entry.env_gate is not None and entry.env_gate not in ENV_GATES:
        raise ValueError(f"tool {entry.name}: unknown env_gate {entry.env_gate!r}")
    unknown = entry.surfaces - SURFACES
    if unknown:
        raise ValueError(f"tool {entry.name}: unknown surfaces {unknown}")
    if entry.name in REGISTRY:
        raise ValueError(f"tool {entry.name}: registered twice")
    REGISTRY[entry.name] = entry
    return entry


def entries() -> tuple[ToolEntry, ...]:
    """Every declared tool, in declaration (= agent serving) order."""
    return tuple(REGISTRY.values())


def by_name(name: str) -> ToolEntry:
    """One entry by tool name (KeyError on unknown — a caller routing by name
    must fail loud, never silently miss a capability)."""
    return REGISTRY[name]


def tool_api_entries() -> tuple[ToolEntry, ...]:
    """Entries served as Tool-API HTTP routes, in declaration order."""
    return tuple(e for e in REGISTRY.values() if "tool_api" in e.surfaces)


def agent_entries() -> tuple[ToolEntry, ...]:
    """Entries eligible for the agent toolset (gates applied by the caller)."""
    return tuple(e for e in REGISTRY.values() if "agent" in e.surfaces)


def mcp_entries() -> tuple[ToolEntry, ...]:
    """Entries advertised on the external MCP surface, in declaration order."""
    return tuple(e for e in REGISTRY.values() if "mcp" in e.surfaces)


def mutating_names() -> frozenset[str]:
    """Every MUTATING tool on the agent surface — the static set the dsh driver
    serializes unconditionally, independent of env gates. Scoped to agent entries
    because it is the dsh driver's contract: an mcp-only tool (attach_file) is
    mutating BY EFFECT but never rides this set."""
    return frozenset(
        e.name for e in REGISTRY.values()
        if e.mutating and "agent" in e.surfaces
    )


def holdable_names() -> frozenset[str]:
    """The subset of mutating_names whose Tool-API request model carries the
    `apply` field — the tools the mid-turn gate can actually HOLD. A tool
    without `apply` (sandbox_bash, import_file, ...) is mutating by effect but
    never holds; the dsh driver must not announce a pending verdict for it (the
    card would claim a call is held that already ran)."""
    return frozenset(
        e.name for e in REGISTRY.values()
        if e.mutating and "agent" in e.surfaces
        and e.request_model is not None
        and "apply" in e.request_model.model_fields
    )


def _is_region_capable(entry) -> bool:
    """True when the tool can be constrained to a pinned text region — DERIVED from
    the request model carrying a `region` field, never a hand-kept list.

    # WHY: the set is exactly {edit_document, append_to_document} today, and the next
    # region-capable tool joins it by declaring the field rather than by someone
    # remembering to edit a set here (the mirrored-literal drift the testing rules
    # call out). Everything else that mutates is, by construction, outside a text
    # region. Lives in the registry (not the route generator) because it is pure
    # declaration introspection — the pin gate and the REGION_TOOLS derivation both
    # read it here, keeping routes out of the taxonomy's reach.
    """
    model = entry.request_model
    return bool(model is not None and "region" in model.model_fields)


def resolve_handler(entry: ToolEntry) -> Callable:
    """Import and return the entry's handler (cached). The handler keeps its
    @track_agent_tool telemetry wrapper — both the HTTP route and in-process
    callers (MCP) go through the SAME wrapped function, exactly as the
    pre-registry decorator stack did."""
    cached = _HANDLERS.get(entry.handler_path)
    if cached is not None:
        return cached
    module_name, _, attr = entry.handler_path.partition(":")
    handler = getattr(importlib.import_module(module_name), attr)
    _HANDLERS[entry.handler_path] = handler
    return handler
