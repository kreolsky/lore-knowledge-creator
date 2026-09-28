"""Tool-API mutating create tool — create_document (unified doc + reference surface).

# ARCH: create_document unifies the deprecated create_reference surface (an
# is_reference=True flag re-routes to the reference primitive). It splits into an
# `auto` path (apply directly via the _common primitives) and a `confirm` path
# (refuse with 409 confirmation_required — the driver asks, then retries with
# the X-Agent-Verdict marker). Import DAG: imports _router + _common only.
"""
from agent.context import get_agent_context
from api_key_auth import agent_author_name
from fastapi import Depends, HTTPException

from access import get_project_access
from models import ToolCreateDocument
from routes.tool_api._common import (
    _apply_create_document_direct,
    _apply_create_reference_direct,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool


@track_agent_tool("create_document")
async def tool_create_document(
    body: ToolCreateDocument, ctx: dict = Depends(get_agent_context),
):
    user = ctx["user"]
    access = await get_project_access(ctx["project_id"], user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Full project access required to create")
    # Placement contract: the
    # three cases split on PRESENCE, read from model_fields_set — omitted =
    # invalid, explicit null = deliberate project root, an id = that parent. The
    # ONE carve-out: under a subtree-scoped key an omitted parent resolves to
    # the scope root deliberately (scope.resolve_scoped_parent — ARCH at
    # agent/collab_writes.py), so the 422 fires only when
    # scope_root is None.
    # INVARIANT: an OMITTED parent_id is a client error under an unscoped key.
    # Why: "neither placement" silently landed at the project root before this
    # guard — the model could not tell a refused intent from an accepted one.
    if "parent_id" not in body.model_fields_set and not ctx.get("scope_root"):
        raise HTTPException(
            status_code=422,
            detail="parent_id is required: pass a parent document id, or null "
                   "for the project root; node_type=\"reference\" requires the "
                   "host as parent_id",
        )
    # node_type is the ONLY kind name, and the ONLY kind name the SCHEMA
    # advertises. The server DERIVES the row's is_reference + media_type from it
    # (a reference leaf with no file part, media_type=markdown) and overwrites
    # whatever the body carried, so a node claiming to be audio/image with no
    # file is unexpressible — the two fields survive on the model as internal
    # derived state, never as agent-settable arguments.
    if body.node_type == "reference":
        if not body.parent_id:
            raise HTTPException(
                status_code=422,
                detail="node_type=\"reference\" requires parent_id "
                       "(the host document)",
            )
        body.is_reference = True
        body.media_type = "markdown"
    else:
        body.is_reference = False

    # normalize_markdown opt-in, applied at the
    # boundary so BOTH the auto and the confirm path write (and the proposal card
    # shows) the SAME normalized text. Default False — agent-authored markdown must
    # NOT be reflowed. The capability moved here from the upload tool's removed
    # `content` path: a caller importing a .md file's text now passes it as
    # create_document(content=…, normalize=True) instead of upload(content=…).
    content = body.content
    if body.normalize and content:
        from markdown_normalize import normalize_markdown

        content = normalize_markdown(content)

    # PR4 R2: unified apply-mode resolver. Create has no system-doc target, so
    # is_system=False (preserving the pre-PR4 create behavior; the is_system cell
    # is genuinely additive for the edit surface only). MCP force-auto applies here
    # too (a read-write key's create always applies).
    if (await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=False,
    )).mode != "auto":
        _refuse_unconfirmable("create_document")
    # ARCH (minimal-tool-set plan): create_document unifies create_reference.
    # When is_reference=True route through the reference primitive (the parent_id
    # is the host document). The standalone create_reference endpoint stays for
    # external callers; new agent calls go through create_document.
    if body.is_reference:
        return await _apply_create_reference_direct(
            document_id=body.parent_id, title=body.title, content=content,
            media_type=body.media_type, source_url=body.source_url,
            project_id=ctx["project_id"], user=user,
            scope_root=ctx.get("scope_root"),
            # S1: the byline is the making key's label (owner's name when the
            # key has none — internal/blank). Rights axis is created_by, unchanged.
            author_name=agent_author_name(ctx),
        )
    return await _apply_create_document_direct(
        title=body.title, content=content, parent_id=body.parent_id,
        project_id=ctx["project_id"], user=user,
        scope_root=ctx.get("scope_root"),
    )
