"""Tool-API image generation launcher — the generate_image route handler.

Subsystem overview, ARCH prose and the SYSTEM marker live in
image_gen/__init__.py. The config gate, the stable-target resolver and the
tool_generate_image route handler live here; the generation itself (the arq
run_generation driver + the /prompt + /history poll) is service code in
image_generation/run.py. The config values resolve through settings AT CALL TIME
(tests patch the config.* bucket — the settings fallback leg); the fetch_one and
enqueue seams resolve in THIS module.
"""

import json
import logging
from uuid import uuid4

import settings
from agent.context import get_agent_context
from comfy_markers import marked_nodes, parse_size
from fastapi import Depends, HTTPException

from access import get_project_access
from db import fetch_one
from jobs import pool as jobs_pool
from models import ToolGenerateImage, is_ref_row
from routes.tool_api_telemetry import track_agent_tool

logger = logging.getLogger(__name__)


async def _require_configured() -> str:
    """503 when ComfyUI is not wired up; returns the live base URL."""
    url = await settings.get("COMFYUI_URL")
    if not url:
        raise HTTPException(status_code=503, detail="Image generation is not configured")
    return url

# ─── Detached generation ───
# ComfyUI generation runs OUT OF the request/turn lifecycle. The tool handler
# (tool_generate_image) is a fast LAUNCHER: it validates the doc + target + the
# count/workflow pairing (everything that can surface a clean HTTP error —
# 400/403/404), mints a run_id, enqueues an arq task with the admin workflow,
# size and prompt template read at launch, and returns {status:"generating"} at once.
#
# ARCH: the generation runs on the arq
# DEFAULT queue, NOT as a web-process asyncio.create_task. Why: the web process is
# reload-volatile (uvicorn --reload on any .py edit, a deploy, a container restart),
# and create_task dies with CancelledError (a BaseException — not caught by
# `except Exception`) — no generate_image_failed, no chip, a spinner that never
# stops. A worker survives those events. max_tries=1 (see generate_image_task): a
# retry re-runs ComfyUI and persists a SECOND image + chip — duplicate output is
# worse than a reported failure. run_generation's CancelledError arm still
# covers a worker SHUTDOWN mid-flight: it emits generate_image_failed then
# re-raises (never swallows cancellation). Concurrency is bounded at the worker via
# the COMFY_CONCURRENCY semaphore, not by a set in the web process.

async def _resolve_target_doc_id(ctx: dict, body: ToolGenerateImage) -> str:
    """B2a: pin the attachment target to the
    chat session's STABLE parent document, not the agent's `document_id` argument.

    The agent reads document_id from the live-open-doc prompt line at tool-call
    time, so a user who switches documents mid-generation moves the target out
    from under the (now detached) call. The chat session's document_id is the
    stable parent the image should always land under. Best-effort + non-breaking:
    a missing/unmatched session falls back to body.document_id (the prior
    behavior), and a mismatch NEVER errors — the security gate remains the
    subsequent doc-ownership validation (doc.project_id != ctx project_id → 404).
    """
    session_id = ctx.get("session_id")
    if session_id:
        try:
            session = await fetch_one("chat_sessions", session_id)
        except Exception:
            session = None
        if (isinstance(session, dict)
                and session.get("project_id") == ctx["project_id"]
                and session.get("user_id") == ctx["user_id"]
                and session.get("document_id")):
            return str(session["document_id"])
    return body.document_id

# INVARIANT: the route path MUST equal the tool name exactly.
# Why: the dsh driver builds the URL generically
# (`${TOOL_API_INTERNAL_URL}/api/tool/${toolName}`) and consults no path map, so a mismatch is a runtime
# 404 the test suite cannot catch without asserting over the served list
# (test_tool_api_routes_match_tool_names). Same invariant as the sandbox routes.

@track_agent_tool("generate_image")
async def tool_generate_image(
    body: ToolGenerateImage, ctx: dict = Depends(get_agent_context),
):
    """Generate an image via ComfyUI and attach it as an image reference to the
    working document.

    The agent's `prompt` — a complete, self-contained scene description — is
    refined server-side into an SD prompt before it reaches ComfyUI.
    DETACHED: this handler is a
    fast LAUNCHER. It validates the doc + target + the count/workflow pairing
    (everything that surfaces a clean HTTP error), mints a run_id, enqueues an
    arq task with the admin workflow, size and prompt template, and returns
    at once:

        {status:"generating", run_id, doc_id}

    The ~10–20s generation runs on the arq DEFAULT queue (NOT a web-process
    task — the web process is reload-volatile and a create_task dies with an
    unreported CancelledError on any .py edit/deploy/restart). The image lands
    asynchronously on the chat's working document (the session's stable parent)
    and is delivered to the chat via the project-WS generate_image_done /
    _failed events; the chips are persisted server-side to messages.gen_steps so a
    reload still shows them (the driver's log never sees the background task). The
    refined SD prompt is NOT in the result (it would land in the agent's context) —
    it travels in the generate_image_done event and that persisted chip.
    """
    await _require_configured()
    # B2a: pin the target to the chat session's stable parent document, not the
    # agent's document_id argument (which can move mid-generation).
    target_doc_id = await _resolve_target_doc_id(ctx, body)
    # Validate the working document BEFORE touching ComfyUI. Doc-first (vs
    # sandbox.py's access-first) is intentional: the 400/404 is the actionable
    # message for the agent and the lookup is cheap — see ComfyUI API flow §2.
    doc = await fetch_one("documents", target_doc_id)
    if not doc or doc.get("deleted_at"):
        raise HTTPException(
            status_code=400,
            detail="generate_image requires a working document; "
                   "open a document in this chat first.",
        )
    if doc.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Document not found")
    # Full project access to create a reference.
    if await get_project_access(ctx["project_id"], ctx["user"]) != "full":
        raise HTTPException(status_code=403, detail="Full project access required")

    # WHY: an image generated while the chat's working document is itself a
    # reference attaches to that reference's PARENT, never to the reference.
    # Why: the schema event documents_parent_check THROWs on a reference parent
    # (surreal/schema.surql), so save_upload's CREATE failed with an unhandled 500
    # AFTER ComfyUI had produced the image — the user saw nothing (prod, m.kozina,
    # chat opened on an audio widget note). One hop is enough: that same event
    # guarantees a reference's parent is never itself a reference. B2a: the hop now
    # starts from the resolved session target (not the agent's document_id arg).
    parent_doc_id = target_doc_id
    if is_ref_row(doc):
        parent_doc_id = doc.get("parent_id")
        parent = await fetch_one("documents", parent_doc_id) if parent_doc_id else None
        if not parent or parent.get("deleted_at"):
            raise HTTPException(
                status_code=400,
                detail="generate_image requires a working document; the open "
                       "reference has no document to attach the image to.",
            )
        if parent.get("project_id") != ctx["project_id"]:
            raise HTTPException(status_code=404, detail="Document not found")

    # The instance Comfy config (admin settings), read live and frozen into the
    # payload so an admin edit between launch and run cannot split one call
    # across two configs. The workflow was validated on write, so it parses.
    comfy = await settings.get_all([
        "COMFYUI_PROMPT", "COMFYUI_WORKFLOW",
        f"COMFYUI_SIZE_{body.orientation.upper()}",
    ])
    wf = json.loads(comfy["COMFYUI_WORKFLOW"])
    if body.count > 1 and not marked_nodes(wf).get("batch"):
        raise HTTPException(
            status_code=400,
            detail=(f"count={body.count} needs a [lore:batch] node in the admin "
                    f"ComfyUI workflow; this workflow makes one image per call — "
                    f"call generate_image once per image."),
        )
    run_id = uuid4().hex
    logger.info(
        "comfy: enqueued run_id=%s (workflow %dB)", run_id,
        len(comfy["COMFYUI_WORKFLOW"]),
    )
    await jobs_pool.enqueue(
        "generate_image_task",
        {
            "run_id": run_id,
            "project_id": ctx["project_id"],
            "user_id": ctx["user_id"],
            # The key-owning user's display name, so the persisted image reference
            # carries author attribution (created_by_name) through the worker path.
            "user_name": (ctx.get("user") or {}).get("name"),
            # S1: the making key's label — the worker's rebuilt ctx carries it so
            # the persisted reference's byline is the agent, not the owner.
            "key_label": ctx.get("key_label"),
            "session_id": ctx.get("session_id") or "",
            "message_id": ctx.get("message_id"),
            "target_doc_id": parent_doc_id,
            "prompt": body.prompt,
            "orientation": body.orientation,
            "count": body.count,
            "prompt_template": comfy["COMFYUI_PROMPT"],
            "workflow": wf,
            "size": parse_size(comfy[f"COMFYUI_SIZE_{body.orientation.upper()}"]),
        },
        job_id=run_id,
    )
    return {"status": "generating", "run_id": run_id, "doc_id": parent_doc_id}
