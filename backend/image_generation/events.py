"""Image generation events — the run's chat-frame pushes + the chip persist.

Subsystem overview: image_generation/__init__.py (see SYSTEM: comfy-image-gen).
The chat-frame push helpers, the BOLA ownership guard
(_message_belongs_to_caller) and the chip-persist UPDATE live here. The emit +
fetch_one seams RESOLVE in this module (image_generation.events.*): the
phase/settled pushers and the chip write call them here.

# ARCH: the detached run's three facts —
# progress, done, failed — are CHAT facts, so they ride the OWNER-FILTERED
# chat channel as backend-minted `lore/image-gen` frames (bus event
# `chat_frame_push` → routes.chat.fanout's subscriber → the same
# {type:'chat_frame', session_id, frame} envelope the turn frames use), never
# a project-wide broadcast (a chat fact reaches the session owner only).
# Every frame is minted from the run's ANCHOR — {seq, turn} of the
# dispatching tool/call, resolved ONCE in the launcher and frozen into the job
# payload — so the worker reads no driver timeline at all.
"""

import logging

from driver.frames import image_gen_running_frame, image_gen_settled_frame

from db import fetch_one
from event_bus import emit

logger = logging.getLogger(__name__)


async def _push_frame(ctx: dict, frame: dict | None) -> None:
    """Best-effort push of ONE minted lore/image-gen frame onto the owner's
    chat channel. Purely best-effort: progress
    and the live card are non-critical UI feedback (a reload still shows the
    settled card from the row's gen_steps) and MUST never break a
    generation. Correlated on session_id."""
    if frame is None:
        return
    try:
        await emit(
            "chat_frame_push",
            project_id=ctx["project_id"],
            session_id=ctx.get("session_id") or "",
            frame=frame,
        )
    except Exception:
        logger.debug(
            "comfy: chat frame push failed", exc_info=True,
        )


async def _push_gen_progress(ctx: dict, run_id: str, phase: str) -> None:
    """Push the run's RUNNING frame for one coarse phase (refining/queued/
    generating/downloading), minted from the payload anchor's phase ladder —
    no replay read per phase."""
    await _push_frame(
        ctx, image_gen_running_frame(ctx.get("anchor") or {}, run_id, phase))


async def _push_gen_settled(ctx: dict, run_id: str, steps: list[dict]) -> None:
    """Push the run's SETTLED frame (done or failed — the step dicts decide),
    built from the SAME gen_steps the persist wrote, through the SAME builder
    the reload attach uses, so the live card equals the reload card by
    construction (the one-mint-site INVARIANT)."""
    await _push_frame(
        ctx, image_gen_settled_frame(ctx.get("anchor") or {}, steps, run_id))


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

    # INVARIANT(data-loss): the detached run's chips are persisted BEFORE the settled
    # frame is pushed. Why: the generation outlives the turn and the driver's log never
    # sees the background task, so this column is the ONLY thing a reload can render
    # them from — push-first would lose them for anyone who reloads in between.
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
