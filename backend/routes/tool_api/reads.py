"""Tool-API read tools — the read-only dispatchers (no mutation, no proposal).

# ARCH: reads reuse the agent read-only executors (readonly_executors) so dsh's HTTP
# path and the in-editor loop return the same shape. Import DAG: imports _router +
# _common only; reaches readonly_executors/scope lazily inside the handlers.
"""
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from models import (
    ToolGetProjectStructure,
    ToolReadDocument,
    ToolSearch,
)
from routes.tool_api_telemetry import track_agent_tool


@track_agent_tool("get_project_structure")
async def tool_get_project_structure(
    body: ToolGetProjectStructure = Body(default_factory=ToolGetProjectStructure),
    ctx: dict = Depends(get_agent_context),
):
    """Return the project's document tree (metadata only — no content).

    Delegates to the shared read-only executor (ARCH: "Reads reuse the agent
    read-only executors") so dsh's HTTP path and the in-editor loop return the
    same shape. See readonly_executors.get_project_structure_tool for the
    topology walk + project_id IDOR invariant.

    Returns a FLAT list; `parent_id` lets the caller reconstruct the tree. A
    start_id call seeds from start_id itself (whole subtree). The ROOT call
    (no start_id) is a bounded orientation layer (default depth 2) whose rows
    carry counters naming everything withheld — drill down with start_id.
    """
    from agent.readonly_executors import get_project_structure_tool
    return await get_project_structure_tool(
        project_id=ctx["project_id"], user=ctx["user"],
        start_id=body.start_id, depth=body.depth,
        include_references=body.include_references,
        include_outline=body.include_outline,
        scope_root=ctx.get("scope_root"),
    )


@track_agent_tool("search_materials")
async def tool_search_materials(
    body: ToolSearch, ctx: dict = Depends(get_agent_context),
):
    from agent.readonly_executors import search_materials_tool
    user = ctx["user"]
    # INVARIANT: every parameter the tool description advertises must reach the
    # executor; a parameter that is display-only is named here with its reason. Why:
    # the drop was invisible for as long as it existed because nothing tied the
    # advertised schema to the request model — `mode` was silently discarded by
    # pydantic's extra=ignore and the chip's presentation layer (the plugin's
    # presentation.ts now) rendered the dropped value back to the
    # user as if it had been honoured. `intent` is display-only (the presentation
    # reads it off the raw args, which never pass through ToolSearch), so it is
    # correctly absent here.
    return await search_materials_tool(
        project_id=ctx["project_id"], user=user, query=body.query,
        corpus=body.corpus, k=body.k,
        mode=body.mode, under_document_id=body.under_document_id,
        scope_root=ctx.get("scope_root"),
    )


@track_agent_tool("read_document")
async def tool_read_document(
    body: ToolReadDocument, ctx: dict = Depends(get_agent_context),
):
    from agent.readonly_executors import read_document_tool
    user = ctx["user"]
    result = await read_document_tool(
        doc_id=body.document_id, project_id=ctx["project_id"], user=user,
        tables=body.tables, table_id=body.table_id,
        offset=body.offset, limit=body.limit,
        scope_root=ctx.get("scope_root"),
    )
    if "error" in result:
        # Map the agent executor's soft errors to HTTP codes (no silent degradation).
        err = result["error"]
        if "not found" in err or "not in this project" in err:
            raise HTTPException(status_code=404, detail=err)
        raise HTTPException(status_code=403, detail=err)
    return result
