"""Tool-API sandbox view — return an image from the caller's workspace to the model.

The bytes ride the same `{image: {media_type, data}}` envelope member as
`view_chat_image`; the dsh plugin's image-result arm (keyed on the registry's
`image_result` declaration) stores them and hands the model an image block.
"""

import base64

from agent.context import get_agent_context
from fastapi import Body, Depends, HTTPException

from models import ToolSandboxViewImage
from routes.tool_api.chat_recall import VIEW_CHAT_IMAGE_MAX_BYTES
from routes.tool_api_telemetry import track_agent_tool

from .files import (
    _require_console_key,
    _require_full_project_access,
    read_workspace_file,
)
from .transport import (
    _require_sandbox_configured,
    _validate_workspace_leaf,
    workspace_for,
)

_UNSUPPORTED_DETAIL = (
    "not a png/jpeg/gif/webp — rasterize first: html or pdf → a Playwright "
    "screenshot; svg → `pip install cairosvg`; other raster formats → convert "
    "with Pillow"
)


def _sniff_media_type(data: bytes) -> str | None:
    """The image type from its magic bytes — the extension is never trusted."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


async def _read_image(ctx: dict, file_path: str) -> bytes:
    """read_workspace_file with this tool's wording on the shared refusals.

    # WHY: files.py stays byte-identical — its 400/403 name `sandbox_path` and its
    # 413 says only "File too large", both shared with import_file's form. The
    # model calls this tool with `file_path`, so the detail is re-worded here;
    # the status codes are unchanged.
    """
    try:
        return await read_workspace_file(ctx, file_path, max_bytes=VIEW_CHAT_IMAGE_MAX_BYTES)
    except HTTPException as exc:
        if exc.status_code == 413:
            raise HTTPException(
                status_code=413,
                detail=f"image is over the {VIEW_CHAT_IMAGE_MAX_BYTES}-byte "
                       f"({VIEW_CHAT_IMAGE_MAX_BYTES // 1024 // 1024} MiB) view limit",
            ) from exc
        if exc.status_code in (400, 403) and isinstance(exc.detail, str):
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.detail.replace("sandbox_path", "file_path"),
            ) from exc
        raise


@track_agent_tool("sandbox_view_image")
async def tool_sandbox_view_image(
    body: ToolSandboxViewImage = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Serve a png/jpeg/gif/webp from the caller's workspace as an image result.

    Returns `{image: {media_type, data}, path}`; anything that is not one of the
    four raster types is a 415 naming the rasterize route that exists on the box.
    """
    _require_console_key(ctx)
    await _require_sandbox_configured()
    await _require_full_project_access(ctx)

    data = await _read_image(ctx, body.file_path)
    media_type = _sniff_media_type(data)
    if media_type is None:
        raise HTTPException(status_code=415, detail=_UNSUPPORTED_DETAIL)
    leaf = _validate_workspace_leaf(body.file_path, workspace_for(ctx["user_id"]))
    return {
        "image": {"media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
        "path": leaf,
    }
