"""MCP Universal Agent Gateway — one MCP server, the whole agent surface.

# SYSTEM: mcp-gateway — the streamable-HTTP MCP endpoint that lets ANY external
# harness (Claude Code, Hermes, anything MCP-capable) connect once and work with
# the project through the existing agent Tool-API, with work products saved back
# as browsable Lore documents.

# ARCH (plan "mcp-universal-agent-gateway"): greenfield module, but NOTHING here
# re-implements business logic. Tool schemas come programmatically from
# the agent_tools registry (never re-hardcoded); dispatch calls the SAME
# executors the Tool-API HTTP surface and the in-editor agent loop use; auth reuses
# the agent-key semantics of agent.context.get_agent_context (Bearer agent_key,
# hash lookup, live project-membership re-check per call). The import DAG is one
# way: mcp_gateway → agent.* / routes.tool_api, NEVER the reverse.
#
# ARCH (plan "mcp-gateway-debt-paydown"): the shared auth resolver lives at the
# backend top level (`api_key_auth.py`), a leaf module consumed by three route
# modules AND this package. It is NOT part of mcp_gateway — importing it here
# would re-couple the package to a concern it merely consumes. The one-way DAG
# above holds: mcp_gateway imports api_key_auth, never the reverse.

# WHY (no privilege escalation): an MCP session acts as the OWNING user of
# the agent key — there is no synthetic identity. Every tool call re-resolves the
# user and re-checks live project membership, exactly like get_agent_context.

# WHY (binary key model — plan "consistent work-area"): an MCP key is either
# read-write (api_keys.auto_apply=True) or read-only (anything else). The MCP
# surface is ALWAYS-AUTO — there is NO proposal/confirmation step and NO
# `proposed` result state over MCP. A read-write key's mutating writes apply
# DIRECTLY (ctx["force_auto_apply"] in dispatch_tool bypasses
# resolve_apply_mode entirely; content edits are reversible via the pre-edit
# checkpoint in the Lore History panel, but structural writes are NOT —
# move_document and rename_document record no checkpoint); a read-only key is rejected (403) at
# dispatch. The agent/human proposal machinery + the apply/reject Tool-API HTTP
# endpoints are NOT exposed over MCP — only the in-app flows use them.

# WHY (no self-approval over MCP): there are NO apply/reject MCP tools and
# NO proposal-polling tool (get_proposal_status was removed). Approval, where it
# exists, happens only in the Lore UI for in-app agent/human proposals.
"""

from mcp_gateway.server import mcp_lifespan

__all__ = ["mcp_lifespan"]
