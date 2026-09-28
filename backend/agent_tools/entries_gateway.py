"""Gateway-only tool declarations — the MCP-surface tools with no Tool-API
route and no agent-surface presence. MCP is a CLIENT of the registry: its tool
list and call routing read THIS table like every other surface.

`request_model` is None for these entries on purpose: their handlers consume
the RAW MCP args dict (they pre-date the registry's uniform (body, ctx)
handler shape and do their own arg validation at the edge — see
mcp_gateway.gateway_tools); the MCP adapter calls them as handler(args, ctx).
Their specs live in agent_tools.specs.gateway (pure data).
"""
from agent_tools.registry import ToolEntry, register
from agent_tools.specs.gateway import (
    ATTACH_FILE_TOOL,
    GET_FILE_TOOL,
    INIT_TOOL,
    PREVIEW_EXTRACTOR_TOOL,
    REPROCESS_FILE_TOOL,
)

_MCP = frozenset({"mcp"})

register(ToolEntry(
    spec=INIT_TOOL, request_model=None,
    handler_path="mcp_gateway.gateway_tools:tool_init",
    surfaces=_MCP,
))
register(ToolEntry(
    spec=GET_FILE_TOOL, request_model=None,
    handler_path="mcp_gateway.gateway_tools:_dispatch_get_file",
    surfaces=_MCP,
))
register(ToolEntry(
    spec=ATTACH_FILE_TOOL, request_model=None,
    handler_path="mcp_gateway.gateway_tools:_dispatch_attach_file",
    surfaces=_MCP,
))
register(ToolEntry(
    spec=REPROCESS_FILE_TOOL, request_model=None,
    handler_path="mcp_gateway.gateway_tools:_dispatch_reprocess_file",
    surfaces=_MCP,
))
register(ToolEntry(
    spec=PREVIEW_EXTRACTOR_TOOL, request_model=None,
    handler_path="mcp_gateway.gateway_tools:_dispatch_preview_extractor",
    surfaces=_MCP,
))
