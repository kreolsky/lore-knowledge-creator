"""Image generation events — phase progress + result/failure broadcasts.

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen).
The project-WS broadcast helpers, the BOLA ownership guard
(_message_belongs_to_caller) and the chip-persist UPDATE live here. The emit +
fetch_one seams RESOLVE in this module (image_generation.events.*): the
phase/done/failed broadcasters call them here.
"""

import logging

from db import fetch_one
from event_bus import emit
from image_generation.image_refine import _RefineResult

logger = logging.getLogger(__name__)

# ─── Live generation phase broadcast ────
# The synchronous tool emits coarse phase events (refining/queued/generating/
# downloading) via the project-WS event bus so the chat can show progress during
# the ~10–20s ComfyUI wait (the agent turn carries no mid-tool-call events). Purely
# best-effort: progress is non-critical UI feedback and MUST never break a
# generation. Correlated on session_id (one active generation per chat session).

_GEN_PHASES = ("refining", "queued", "generating", "downloading")

async def _emit_gen_progress(ctx: dict, run_id: str, phase: str) -> None:
    try:
        await emit(
            "generate_image_progress",
            project_id=ctx["project_id"],
            session_id=ctx.get("session_id") or "",
            run_id=run_id,
            message_id=ctx.get("message_id") or "",
            phase=phase,
        )
    except Exception:
        logger.debug("comfy: progress emit failed (phase=%s)", phase, exc_info=True)

async def _emit_gen_done(
    ctx: dict, run_id: str, message_id: str | None,
    reference_ids: list[str], title: str, refine: _RefineResult,
    steps: list[dict],
) -> None:
    """Best-effort delivery of the generation result to the chat over the
    project-WS. The refiner outcome AND the
    already-built step dicts travel HERE — single source of truth (the Redis stash
    is gone) — so the chat stamps the SAME chips the server persisted to
    `messages.gen_steps` (no backend↔frontend reconstruction drift)."""
    try:
        await emit(
            "generate_image_done",
            project_id=ctx["project_id"],
            session_id=ctx.get("session_id") or "",
            run_id=run_id,
            message_id=message_id or "",
            reference_ids=reference_ids,
            title=title,
            refine={"prompt": refine.prompt, "ok": refine.ok, "error": refine.error},
            steps=steps,
        )
    except Exception:
        logger.debug("comfy: done emit failed (run_id=%s)", run_id, exc_info=True)

async def _emit_gen_failed(
    ctx: dict, run_id: str, message_id: str | None, error: str,
) -> None:
    """Best-effort failure delivery (no-silent-degradation): a background
    generation that raised must reach the chat, never die silently."""
    try:
        await emit(
            "generate_image_failed",
            project_id=ctx["project_id"],
            session_id=ctx.get("session_id") or "",
            run_id=run_id,
            message_id=message_id or "",
            error=error,
        )
    except Exception:
        logger.debug("comfy: failed emit failed (run_id=%s)", run_id, exc_info=True)

async def _message_belongs_to_caller(
    message_id: str, project_id: str, user_id: str,
) -> bool:
    """BOLA guard: `message_id` arrives from
    the CLIENT-CONTROLLED X-Agent-Message-Id header, so appending chips to it by id
    alone is an object-level auth gap — a holder of any project agent key could
    target a victim message id in a project they do not own. `messages` has no
    project_id, so resolve ownership via messages.chat_id → chat_sessions and
    assert it matches the caller's project + user (the same pairing
    `_resolve_target_doc_id` validates for the session). Never raises; a miss drops
    the chip write (the image reference itself is still persisted on the document)."""
    try:
        msg = await fetch_one("messages", message_id)
        chat_id = msg.get("chat_id") if isinstance(msg, dict) else None
        if not chat_id:
            return False
        session = await fetch_one("chat_sessions", chat_id)
        return bool(
            isinstance(session, dict)
            and session.get("project_id") == project_id
            and session.get("user_id") == user_id
        )
    except Exception:
        logger.debug(
            "comfy: message ownership check failed for msg=%s", message_id,
            exc_info=True,
        )
        return False

async def _persist_gen_steps(ctx: dict, message_id: str | None, steps: list[dict]) -> None:
    """Append the detached generation's chips to the assistant message's
    `gen_steps` column via a targeted Surreal UPDATE.

    # INVARIANT(data-loss): the detached run's chips are persisted BEFORE the done/failed
    # emit. Why: the generation outlives the turn and the driver's log never sees
    # the background task, so this column is the ONLY thing a reload can render
    # them from — emit-first would lose them for anyone who reloads in between.
    Appends (array::concat) rather than replaces: two runs in one turn each write
    their own chips. Best-effort — a miss leaves the chips absent on reload but the
    image reference itself is always persisted on the document via save_upload
    (recoverable from the doc's References)."""
    if not message_id:
        return
    if not await _message_belongs_to_caller(
        message_id, ctx.get("project_id") or "", ctx.get("user_id") or "",
    ):
        logger.warning("comfy: chip write skipped, message not owned by caller msg=%s", message_id)
        return
    try:
        from db import get_db
        db = await get_db()
        await db.query(
            "UPDATE type::record('messages', $mid) SET "
            "gen_steps = array::concat(gen_steps ?? [], $steps)",
            {"mid": message_id, "steps": steps},
        )
    except Exception:
        logger.warning("comfy: chip persist failed msg=%s", message_id, exc_info=True)
