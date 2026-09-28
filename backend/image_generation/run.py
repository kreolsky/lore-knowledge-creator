"""Image generation driver — the arq run_generation driver + the ComfyUI poll.

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen).
The launcher (routes/tool_api/image_gen/tool.py) enqueues generate_image_task;
the worker (jobs/tasks/comfy.py) calls run_generation here. The config values
(COMFYUI_URL / COMFYUI_TIMEOUT_S) resolve through settings AT CALL TIME in THIS
module (tests patch the config.* bucket — the settings fallback leg); the
_refine_prompt and secrets.randbelow seams resolve here too (run_generation
imports _refine_prompt from .refine, so the worker-path patch binds here). The
ComfyUI client is the shared pool's "comfy" entry (SYSTEM: http-clients) — tests
fake it through the one http_pool seam, not this module.
"""

import asyncio
import logging
import secrets
import time

import http_clients
import httpx
import settings
from comfy_markers import fill_workflow

from models import ToolGenerateImage

from .events import _emit_gen_progress
from .image_comfy import _PER_REQUEST_TIMEOUT, _GenError
from .persist import _announce_failure, _persist_and_announce
from .refine import _refine_prompt
from .workflow import _download_outputs, _extract_images

logger = logging.getLogger(__name__)

# Poll /history with exponential backoff (1s → ×1.5 → … → this cap), clamped to
# the remaining COMFYUI_TIMEOUT_S so the last sleep never overshoots. A typical
# 10–20s gen stays responsive (detected within ~5s of completion); a worst-case
# 120s wait drops from ~60 polls to ~20.
_MAX_POLL_INTERVAL_S = 5.0
_POLL_GROWTH = 1.5


async def run_generation(
    ctx: dict, run_id: str, target_doc_id: str, body: ToolGenerateImage,
    prompt_template: str, wf: dict, size: list[int] | None,
) -> None:
    """ComfyUI generation.

    Lives on the arq worker — ONE implementation shared by web (inline tests)
    and worker. The body is split into resolve/enqueue/download/persist helpers;
    this driver keeps only the try/except skeleton. NEVER raises out except
    CancelledError: every failure path emits generate_image_failed + logs (no-silent-
    degradation). D3: a worker shutdown cancels the task — report it (never a hung
    spinner) then RE-RAISE so cancellation propagates (never swallow it).
    """
    title = body.prompt.strip()[:60] or "Generated image"
    message_id = ctx.get("message_id")
    logger.info("comfy: generate start run_id=%s target=%s count=%d msg=%s",
                run_id, target_doc_id, body.count, message_id)
    try:
        if size is None:
            # WHY: only a job enqueued before the launcher sent `size` lands here;
            # its graph carries no markers, so filling it would render the graph's
            # own baked-in prompt. Fail it visibly instead.
            raise _GenError(409, "Image generation was queued before an upgrade; retry it")
        await _emit_gen_progress(ctx, run_id, "refining")
        refine = await _refine_prompt(body.prompt.strip(), prompt_template)
        # WHY the seed is drawn here: ComfyUI does NOT randomize a seed passed in
        # an API graph, so a fresh one per call is what varies the image.
        graph = fill_workflow(
            wf, prompt=refine.prompt, seed=secrets.randbelow(2**31), size=size,
            batch=body.count, run_id=run_id,
        )
        client = http_clients.get_http_client("comfy", timeout=_PER_REQUEST_TIMEOUT)
        images = await _enqueue_and_poll(ctx, run_id, client, graph)
        outputs = await _download_outputs(ctx, run_id, client, images)
        await _persist_and_announce(ctx, run_id, target_doc_id, title, message_id, refine, outputs)
    except asyncio.CancelledError:
        # BaseException (not caught by `except Exception`) — report + re-raise.
        logger.warning("comfy: generate CANCELLED run_id=%s", run_id)
        await _announce_failure(ctx, run_id, message_id, "Image generation was cancelled")
        raise
    except _GenError as exc:
        logger.warning("comfy: generate FAILED run_id=%s code=%s detail=%s", run_id, exc.status_code, exc.detail)
        await _announce_failure(ctx, run_id, message_id, exc.detail)
    except Exception as exc:
        logger.exception("comfy: generate unexpected error run_id=%s", run_id)
        await _announce_failure(ctx, run_id, message_id, f"Image generation failed: {exc}")

async def _enqueue_and_poll(
    ctx: dict, run_id: str, client: httpx.AsyncClient, wf: dict,
) -> list[dict]:
    """POST the filled workflow to ComfyUI /prompt, poll /history until it
    completes, then return every saved image. Raises _GenError
    on ComfyUI unavailability / execution error / timeout / missing output. Each
    /history GET rides the per-request client timeout; COMFYUI_TIMEOUT_S bounds the
    loop."""
    comfy = await settings.get_all(["COMFYUI_URL", "COMFYUI_TIMEOUT_S"])
    try:
        r = await client.post(f"{comfy['COMFYUI_URL']}/prompt", json={"prompt": wf})
    except httpx.HTTPError as exc:
        raise _GenError(503, f"ComfyUI is not available: {exc}") from exc
    if r.status_code != 200:
        raise _GenError(503, "ComfyUI is not available")
    prompt_id = r.json()["prompt_id"]
    await _emit_gen_progress(ctx, run_id, "queued")
    await _emit_gen_progress(ctx, run_id, "generating")
    deadline = time.monotonic() + comfy["COMFYUI_TIMEOUT_S"]
    history_body: dict | None = None
    poll_interval = 1.0
    while time.monotonic() < deadline:
        h = await client.get(f"{comfy['COMFYUI_URL']}/history/{prompt_id}")
        if h.status_code == 200:
            body_json = h.json()
            if prompt_id in body_json:
                status = body_json[prompt_id].get("status", {})
                if status.get("status_str") == "error":
                    raise _GenError(
                        502, f"ComfyUI generation failed: {status.get('messages')}",
                    )
                if status.get("completed"):
                    history_body = body_json
                    break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))
        poll_interval = min(poll_interval * _POLL_GROWTH, _MAX_POLL_INTERVAL_S)
    if history_body is None:
        raise _GenError(504, "Image generation timed out")
    return _extract_images(history_body, prompt_id)
