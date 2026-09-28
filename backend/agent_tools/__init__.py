"""The agent-tools package — the ONE tool registry.

# SYSTEM: agent-tools — single declaration per agent tool; every surface is
#   derived (agent toolset, Tool-API routes, MCP tool list).

# ARCH: a tool is declared ONCE in `entries.py`
# (name+schema spec, request model, handler, `mutating`, surfaces, optional agent
# env gate). Derived from that one declaration:
#   - `AGENT_TOOLS` / `MUTATING_TOOLS` / `agent_toolset()` (agent.tools)
#   - the Tool-API `@router.post` routes (agent_tools.routing, bound by
#     routes/tool_api/__init__.py)
#   - the MCP tool list and its routing (mcp_gateway)
# The package lives OUTSIDE routes.chat on purpose: the Tool-API and the MCP
# gateway are not chat surfaces, and they import into this package — never the
# reverse.

Import DAG: registry.py is a pure leaf (stdlib only); specs/ is pure data;
entries.py imports models (pydantic leaf) + registry; routing.py imports only
registry at module level. Nothing here imports routes.* at module level.
"""
# Population imports — MUST follow `registry` so REGISTRY is filled the moment
# any `from agent_tools.registry import …` resolves (the package __init__ runs
# first, so every consumer sees the populated table; no import-order hazard).
from agent_tools import entries as _entries  # noqa: F401,E402  (side-effect: registers)
from agent_tools import (
    entries_gateway as _entries_gateway,  # noqa: F401,E402  (side-effect)
)
from agent_tools import registry  # noqa: F401  (re-exported namespace)

__all__ = ["registry"]
