"""ComfyUI image generation task — the arq-resident half of SYSTEM: comfy-image."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Bounds concurrent generate_image runs on the
# worker so a burst of tool calls queues in arq instead of hammering one ComfyUI
# box. Lazily created on first use inside the running loop, and RE-CREATED if the
# running loop changed (production: one long-lived worker loop; tests: a fresh loop
# per case — a semaphore bound to a prior loop errors on acquire).
_comfy_semaphore: asyncio.Semaphore | None = None
_comfy_semaphore_loop: object | None = None


def _get_comfy_semaphore() -> asyncio.Semaphore:
    global _comfy_semaphore, _comfy_semaphore_loop
    loop = asyncio.get_running_loop()
    if _comfy_semaphore is None or _comfy_semaphore_loop is not loop:
        from config import COMFY_CONCURRENCY
        _comfy_semaphore = asyncio.Semaphore(COMFY_CONCURRENCY)
        _comfy_semaphore_loop = loop
    return _comfy_semaphore


# Why max_tries=1 on generate_image_task: a retry re-runs ComfyUI and persists a
# SECOND image + chip; duplicate output is worse than a reported failure, so the
# task is terminal on the first try (run_generation pushes the failed chat
# frame on every failure path). The registration guard (it MUST be in WorkerSettings.functions)
# lives in COMFY_TASK_NAMES + jobs.worker.validate_queue_config — see the frozenset
# comment below. The payload contract (built by the launcher, rebuilt here) is
# documented inline in generate_image_task.


def _rebuild_ctx(payload: dict) -> dict:
    """The minimal ctx run_generation needs for the chat-frame pushes + the
    BOLA check — never the full agent-key context (it carries nothing the
    generation can use, and reconstructing it would couple the worker to
    auth). The run's anchor rides it: resolved by the LAUNCHER over the
    driver replay, frozen into the payload — every lore/image-gen chat frame
    the run pushes is minted from it, so the worker reads no timeline."""
    return {
        "project_id": payload["project_id"],
        "user_id": payload["user_id"],
        "user_name": payload.get("user_name"),
        # S1: the making agent key's label, frozen into the payload at enqueue
        # (the worker has no key context) — the persisted reference's byline.
        "key_label": payload.get("key_label"),
        "session_id": payload.get("session_id") or "",
        "message_id": payload.get("message_id"),
        "call_id": payload.get("call_id"),
        "anchor": payload.get("anchor"),
        "user": {"id": payload["user_id"]},
    }


async def generate_image_task(ctx, payload: dict) -> None:
    """arq-resident ComfyUI image generation.

    The web process is reload-volatile, so a detached asyncio.create_task there dies
    on any .py edit/deploy/restart with CancelledError (a BaseException, not caught
    by `except Exception`) — no failure event, a spinner that never stops. The
    launcher enqueues this task with the admin workflow + size + prompt template
    it read at launch (re-reading them in the worker reopens a divergence window);
    here we rebuild the minimal ctx + ToolGenerateImage and call the ONE shared
    run_generation (web + worker share it).

    Concurrency is bounded by the COMFY_CONCURRENCY semaphore: a burst of tool
    calls queues in arq instead of hammering one ComfyUI box.
    """
    async with _get_comfy_semaphore():
        from image_generation.run import run_generation

        from models import ToolGenerateImage

        body = ToolGenerateImage(
            prompt=payload["prompt"],
            document_id=payload["target_doc_id"],
            orientation=payload.get("orientation", "square"),
            count=payload.get("count", 1),
        )
        # WHY .get("size"): a job enqueued by a launcher that predates it
        # carries none; run_generation refuses it with the run's failed frame
        # (a card the user sees) instead of a KeyError here that reports nothing.
        await run_generation(
            _rebuild_ctx(payload), payload["run_id"], payload["target_doc_id"], body,
            payload.get("prompt_template") or "", payload["workflow"],
            payload.get("size"),
        )


# INVARIANT: every Comfy image-gen task that MUST be registered on the default worker.
# Why: arq silently drops a job whose function is not registered on the polled worker
# ("enqueued but never run"). A new generate_image_*_task added here but forgotten in
# WorkerSettings.functions must crash the worker at startup (validate_queue_config in
# jobs/worker.py) instead of silently dropping every enqueued generation — the same
# failure BACKUP_TASK_NAMES guards for backup tasks. For image-gen specifically, a
# dropped generate_image_task leaves a spinner that never clears — the exact
# failure running it on arq exists to prevent.
COMFY_TASK_NAMES = frozenset({
    "generate_image_task",
})
