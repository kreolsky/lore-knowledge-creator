"""Image generation outputs — the /history image collector + /view download.

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen);
the graph is filled by comfy_markers.fill_workflow. The config seams
(MAX_IMAGE_SIZE_MB via settings.get, COMFYUI_URL) and the _emit_gen_progress call
resolve in THIS module (image_generation.workflow.*).
"""

import httpx
import settings
from files_util import detect_image_mime

from image_generation.image_comfy import _GenError

from .events import _emit_gen_progress


def _extract_images(history_body: dict, prompt_id: str) -> list[dict]:
    """Collect every saved image of a completed /history body, across ALL output
    nodes. Only `type == "output"` entries count: preview nodes report `temp`
    images, which are not the result."""
    outputs = history_body[prompt_id].get("outputs", {})
    images = [
        img
        for node_output in outputs.values() if isinstance(node_output, dict)
        for img in node_output.get("images") or []
        if isinstance(img, dict) and img.get("type") == "output"
    ]
    if not images:  # a batch may report success but yield zero outputs → fail loudly
        raise _GenError(502, "ComfyUI returned no images")
    return images

async def _download_outputs(
    ctx: dict, run_id: str, client: httpx.AsyncClient, images: list[dict],
) -> list[tuple[bytes, str]]:
    """Download + validate EVERY batch output via /view BEFORE any persistence so a
    mid-batch failure leaves no orphaned references. Returns (bytes, mime) pairs;
    mime is detected from magic bytes (ComfyUI /view carries no MIME)."""
    await _emit_gen_progress(ctx, run_id, "downloading")
    comfy_url = await settings.get("COMFYUI_URL")
    outputs: list[tuple[bytes, str]] = []
    for out in images:
        view = await client.get(
            f"{comfy_url}/view",
            params={
                "filename": out["filename"],
                "subfolder": out.get("subfolder", ""),
                "type": out.get("type", "output"),
            },
        )
        if view.status_code != 200:
            raise _GenError(502, "Failed to download generated image")
        data = view.content
        mime = detect_image_mime(data)
        if mime is None:
            raise _GenError(
                502, "Generated output is not a recognized image (png/webp/jpeg/gif)",
            )
        max_image_mb = await settings.get("MAX_IMAGE_SIZE_MB")
        if len(data) > max_image_mb * 1024 * 1024:  # save_upload does NOT enforce size
            raise _GenError(413, f"Generated image too large (max {max_image_mb}MB)")
        outputs.append((data, mime))
    return outputs
