"""Tool-API read tools — the read-only dispatchers (no mutation, no proposal).

# ARCH: reads reuse the agent read-only executors (readonly_executors) so dsh's HTTP
# path and the in-editor loop return the same shape. Import DAG: imports _router +
# _common only; reaches readonly_executors/scope lazily inside the handlers.
"""
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from models import (
    ToolDiffDocumentVersions,
    ToolGetLinks,
    ToolGetProjectStructure,
    ToolListDocumentVersions,
    ToolReadDocument,
    ToolSearch,
    ToolViewReferenceImage,
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


@track_agent_tool("get_links")
async def tool_get_links(
    body: ToolGetLinks, ctx: dict = Depends(get_agent_context),
):
    """Typed links of a document — both directions, every kind (plan typed-edges).

    Delegates to the SAME service the /edges route reads (one function, one
    default kind set); the executor applies the read gates (same-project, RBAC,
    scope ceiling, page lock) and the service walls the peers.
    """
    from agent.readonly_executors import get_links_tool
    result = await get_links_tool(
        doc_id=body.document_id, project_id=ctx["project_id"], user=ctx["user"],
        direction=body.direction, kinds=body.kinds,
        scope_root=ctx.get("scope_root"),
    )
    if "error" in result:
        # Map the executor's soft errors to HTTP codes (no silent degradation).
        err = result["error"]
        if "not found" in err or "not in this project" in err:
            raise HTTPException(status_code=404, detail=err)
        raise HTTPException(status_code=403, detail=err)
    return result


@track_agent_tool("read_document")
async def tool_read_document(
    body: ToolReadDocument, ctx: dict = Depends(get_agent_context),
):
    from agent.readonly_executors import read_document_tool
    user = ctx["user"]
    result = await read_document_tool(
        doc_id=body.document_id, project_id=ctx["project_id"], user=user,
        tables=body.tables, table_id=body.table_id,
        offset=body.offset, limit=body.limit, version=body.version,
        scope_root=ctx.get("scope_root"),
    )
    return _raise_soft_error(result)


@track_agent_tool("view_reference_image")
async def tool_view_reference_image(
    body: ToolViewReferenceImage, ctx: dict = Depends(get_agent_context),
):
    """The pixels of an image reference, pressed to the model cap, as an image result.

    Gates: the read_document chain (resolve → RBAC + scope + lock), then the file
    through resolve_reference_file bound to the key's project — a foreign-project
    id answers the same 404 as an unknown one (see its INVARIANT(security)).
    """
    import asyncio
    from pathlib import Path

    from agent.history_executors import gated_target
    from files_service import resolve_reference_file

    from routes.tool_api._image_view import image_envelope
    target = await gated_target(
        doc_id=body.document_id, project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"),
    )
    if isinstance(target, dict):
        if "not in this project" in target.get("error", ""):
            # INVARIANT(security): a foreign-project id answers the SAME 404 as an unknown id.
            # Why: a distinguishable detail is an existence oracle across the project boundary.
            target = {"error": "Document not found"}
        _raise_soft_error(target)
    doc = target.doc
    if doc.get("media_type") != "image" or not doc.get("file_path"):
        raise HTTPException(status_code=400,
                            detail="not an image reference — read it with read_document")
    abs_path, _mime, _name = await resolve_reference_file(
        ref_id=target.resolved, project_id=ctx["project_id"],
    )
    raw = await asyncio.to_thread(Path(abs_path).read_bytes)
    envelope = await image_envelope(raw, unsupported_detail="image format is not png/jpeg/gif/webp")
    return {**envelope, "document_id": target.resolved, "title": doc.get("title") or ""}


def _raise_soft_error(result: dict) -> dict:
    """Map an executor's soft error to HTTP (no silent degradation); pass a result through."""
    if "error" in result:
        err = result["error"]
        if "not found" in err or "not in this project" in err:
            raise HTTPException(status_code=404, detail=err)
        raise HTTPException(status_code=403, detail=err)
    return result


@track_agent_tool("list_document_versions")
async def tool_list_document_versions(
    body: ToolListDocumentVersions, ctx: dict = Depends(get_agent_context),
):
    from agent.history_executors import list_document_versions_tool
    return _raise_soft_error(await list_document_versions_tool(
        doc_id=body.document_id, project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"), limit=body.limit,
        before_checkpoint_id=body.before_checkpoint_id,
    ))


@track_agent_tool("diff_document_versions")
async def tool_diff_document_versions(
    body: ToolDiffDocumentVersions, ctx: dict = Depends(get_agent_context),
):
    from agent.history_executors import diff_document_versions_tool
    return _raise_soft_error(await diff_document_versions_tool(
        doc_id=body.document_id, project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"), from_checkpoint_id=body.from_checkpoint_id,
        to=body.to,
    ))
