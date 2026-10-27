"""Tool-API subtree tools — export_subtree_md / import_subtree_md.

The surface half (auth + staged-zip transport) of the subtree Markdown tools;
the tree ⇄ folder domain mapper lives in backend/subtree_md.py, the staged
# artifacts + signed tokens in mcp_gateway/staging.py.

# ARCH: export is NON-mutating (the staged zip is ephemeral, so a read-only
# key may call it); import is mutating (the dispatch read-only-key gate and
# the force-auto apply are the registry's, same as every base mutating tool)
# and auto-only — the bytes are user-supplied data, not a text edit to
# confirm (the import_file posture: no `apply` field, never held). Both read
# ctx.get("scope_root"): export refuses a root outside the key's subtree,
# import resolves its parent through resolve_scoped_parent and writes only
# inside the scope.
"""
from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException
from subtree_md import export_subtree, import_subtree_zip

from access import get_document_access, get_project_access
from db import fetch_one
from models import ToolExportSubtreeMd, ToolImportSubtreeMd, is_ref_row
from routes.tool_api_telemetry import track_agent_tool
from scope import require_doc_in_scope, resolve_scoped_parent


async def _gate_export_root(root_id: str, ctx: dict) -> None:
    """404 (missing/deleted/cross-project/reference root) → per-doc RBAC on
    the root → the subtree scope ceiling — the read-executor gate order."""
    root = await fetch_one("documents", root_id)
    if (
        not root or root.get("deleted_at")
        or root.get("project_id") != ctx["project_id"]
        or is_ref_row(root)
    ):
        raise HTTPException(status_code=404, detail="Document not found")
    if not await get_document_access(root_id, ctx["user"]):
        raise HTTPException(status_code=403, detail="No access to this document")
    await require_doc_in_scope(ctx.get("scope_root"), root_id)


async def _stage_export(result: dict) -> tuple[str, str]:
    """Stage the built zip + mint the signed URL token → (path, expires_iso).

    The mint sweeps first, so a staged artifact never outlives its URL by more
    than one sweep cadence."""
    from mcp_gateway.staging import (
        mint_staged_download_token,
        new_artifact_id,
        stage_artifact,
        sweep_staging,
    )

    sweep_staging()
    artifact_id = new_artifact_id("export")
    stage_artifact(artifact_id, result["zip_bytes"])
    token, expires_iso = await mint_staged_download_token(
        artifact_id, filename=result["filename"],
    )
    return f"/api/mcp/staged/{token}", expires_iso


@track_agent_tool("export_subtree_md")
async def tool_export_subtree_md(
    body: ToolExportSubtreeMd = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Export a document's whole subtree as a zip of Markdown files
    (GitHub/Obsidian-compatible layout) and return a short-lived download URL
    — never bytes."""
    await _gate_export_root(body.root_id, ctx)
    result = await export_subtree(
        root_id=body.root_id, project_id=ctx["project_id"],
    )
    path, expires_iso = await _stage_export(result)
    base = ctx.get("base_url") or ""
    return {
        "status": "ok",
        "root_id": result["root_id"],
        "documents": result["documents"],
        "files": result["files"],
        "filename": result["filename"],
        "url": base + path if base else path,
        "path": path,
        "expires_at": expires_iso,
    }


async def _validate_import_parent(parent_id: str, ctx: dict) -> None:
    """404 on a missing/deleted/cross-project parent, 400 on an attached node
    (a subtree cannot land under a reference), then the scope wall."""
    parent = await fetch_one("documents", parent_id)
    if not parent or parent.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Parent document not found")
    if is_ref_row(parent):
        raise HTTPException(
            status_code=400,
            detail="A subtree imports under a document, not an attached node",
        )
    await require_doc_in_scope(ctx.get("scope_root"), parent_id)


async def _mint_import_upload(ctx: dict, parent_id: str | None) -> dict:
    """Mint the staging upload URL (the import's byte channel — no tool
    accepts bytes as an argument). Authorization is frozen into the token:
    the redeem route has no key, so project, parent, user and scope ride the
    claims (the attach_file mint posture)."""
    import settings
    from mcp_gateway.staging import mint_staged_upload_token, sweep_staging
    from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX

    sweep_staging()
    token, expires_iso = await mint_staged_upload_token(
        project_id=ctx["project_id"], parent_id=parent_id or "",
        user_id=ctx["user_id"], agent_label=ctx.get("key_label"),
        scope_root=ctx.get("scope_root") or "",
    )
    path = MCP_UPLOAD_ROUTE_PREFIX + token
    base = ctx.get("base_url") or ""
    url = base + path if base else path
    max_mb = await settings.get("MAX_ARCHIVE_SIZE_MB")
    return {
        "status": "upload_pending",
        "url": url,
        "path": path,
        "method": "POST",
        "field_name": "file",
        "max_size_mb": max_mb,
        "expires_at": expires_iso,
        "example": f'curl -X POST "{url}" -F "file=@subtree.zip"',
    }


async def _read_workspace_zip(ctx: dict, sandbox_path: str) -> bytes:
    """Console byte channel: read a workspace .zip over SFTP (the console-key
    gate rides inside read_workspace_file), capped by the archive setting."""
    import settings

    from routes.tool_api.sandbox.files import read_workspace_file

    max_mb = await settings.get("MAX_ARCHIVE_SIZE_MB")
    data = await read_workspace_file(
        ctx, sandbox_path, max_bytes=max_mb * 1024 * 1024,
    )
    if not data:
        raise HTTPException(status_code=400, detail="Empty zip — no bytes read")
    return data


@track_agent_tool("import_subtree_md")
async def tool_import_subtree_md(
    body: ToolImportSubtreeMd = Body(default_factory=ToolImportSubtreeMd),
    ctx: dict = Depends(get_agent_context),
):
    """Import an md-folder zip (Obsidian-shaped) as a subtree under parent_id.

    No zip source ⇒ MINT: return the short-lived upload URL to POST it to.
    `staged_id` ⇒ walk the staged artifact; `sandbox_path` (console) ⇒ read
    the workspace zip. Applies immediately (auto-only, the import_file
    posture); every created document goes through the shared create factory,
    so parent validity, the scope wall and unique paths are its own gates.
    """
    user = ctx["user"]
    if await get_project_access(ctx["project_id"], user) != "full":
        raise HTTPException(
            status_code=403, detail="Full project access required to create",
        )
    parent_id = resolve_scoped_parent(ctx.get("scope_root"), body.parent_id)
    if parent_id:
        await _validate_import_parent(parent_id, ctx)

    if body.staged_id is None and body.sandbox_path is None:
        return await _mint_import_upload(ctx, parent_id)
    if body.staged_id is not None:
        from mcp_gateway.staging import staged_path

        zip_bytes = staged_path(body.staged_id).read_bytes()
    else:
        zip_bytes = await _read_workspace_zip(ctx, body.sandbox_path)
    return await import_subtree_zip(
        zip_bytes, project_id=ctx["project_id"], parent_id=parent_id,
        user=user, scope_root=ctx.get("scope_root"),
    )
