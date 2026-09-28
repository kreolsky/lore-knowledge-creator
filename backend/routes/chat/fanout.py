"""Chat frame fan-out — the driver-owned turn's browser hop.

# SYSTEM: chat-fanout — chat frames from the driver channel onto the lifecycle project WS, owner-filtered

Frames the driver channel relays for a chat session reach the
browser over the PROJECT lifecycle WS — never the multiplexed collab channel
(its join is documents-table-bound) — enveloped as
``{type:'chat_frame', session_id, frame}`` with the frame VERBATIM: the
relay arms' own dicts, so the browser's ONE assembler and frame reducer
consume live and reload frames unchanged (parity is architectural).

# ARCH: owner-filtered by construction. AI chats are owner-only
# (routes/chat/sessions.py `_require_session_access`); the fan-out resolves
# the recipient ONCE from the row (`user_id`) and sends through
# ProjectSession.send_to_user — chat frames NEVER ride broadcast, so a second
# project member's socket is outside the delivery set entirely. The
# mid-turn-revocation window is one turn: an open turn outlives the
# membership check, and the next turn's REST gate is where the revoked
# member stops.

# ARCH: ONE pump task per chat session, forwarding the channel's delivery
# order one send at a time — nothing here buffers, merges or reorders.
# Backpressure = drop + client resync: a listener past the channel's queue
# bound is closed with the None sentinel (the pump exits; a later
# ensure_fanout re-attaches — the subscription outlives the listener), and a
# browser that missed frames rebuilds through the reload path (the replay
# projection).

# ARCH: the verdict relay rides the SAME frames — `lore/verdict-ask` relays
# like any other frame (its live card path is this envelope), the answer's
# path is POST /verdicts (unchanged REST), and the card's dismissal is the
# settled tool/result frame. No verdict-specific push exists or is needed.

Callers own the access gate: ensure_fanout runs on the turn path AFTER
`_require_session_access`; this module
is transport wiring, not an authorization surface.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from driver.channel import get_driver_channel

from db import fetch_one
from routes.project_ws import send_to_project_user

logger = logging.getLogger(__name__)


@dataclass
class _Fanout:
    """One session's pump registration (the single writer)."""

    task: asyncio.Task
    detach: Callable[[], None]
    #: The DRIVER session id the channel is subscribed under (compacted_from
    #: for continuation chats); the WS envelope still carries the CHAT id.
    driver_session_id: str


#: session_id → the live pump. ensure_fanout is idempotent against it, and a
#: pump's exit (None sentinel, channel close) removes its own entry.
_pumps: dict[str, _Fanout] = {}


async def ensure_fanout(
    session_id: str, driver_session_id: str | None = None,
) -> bool:
    """Idempotently wire ONE chat session's relayed frames onto its owner's
    project-WS sockets.

    True when the fan-out is (or became) active; False when the session does
    not qualify — missing/deleted row or note row. Raises whatever
    channel.subscribe raises: a session whose channel cannot be opened is
    the caller's explicit turn failure, never a silent no-fan-out turn.

    ``driver_session_id`` is the id the DRIVER knows the
    session by — ``compacted_from or session_id`` for continuation chats,
    whose turns must continue the SOURCE dsh session (the compaction
    continuation ARCH in completions.py). The channel subscribes and binds
    under IT; the pump registry and the WS envelope stay keyed by the CHAT
    session id (the browser dispatches on that).
    """
    if session_id in _pumps:
        return True
    session = await fetch_one("chat_sessions", session_id)
    if (
        not session
        or session.get("deleted_at")
        or session.get("is_note") is True
    ):
        return False
    project_id = str(session.get("project_id") or "")
    owner_id = str(session.get("user_id") or "")
    drv = driver_session_id or session_id
    channel = get_driver_channel()
    await channel.subscribe(drv)
    queue, detach = channel.add_listener(drv)
    # subscribe awaits the driver ack — re-check the registry after it: a
    # concurrent ensure for the same session may have registered its pump
    # while this one waited (the await is the only interleaving window).
    if session_id in _pumps:
        detach()
        return True
    _pumps[session_id] = _Fanout(
        task=asyncio.ensure_future(
            _pump(session_id, project_id, owner_id, queue, detach)),
        detach=detach,
        driver_session_id=drv,
    )
    return True


async def stop_fanout(
    session_id: str, driver_session_id: str | None = None,
) -> None:
    """Stop the session's fan-out: unsubscribe the channel (the None sentinel
    closes the pump; an open turn takes the unsubscribe semantics — partial +
    disconnected halt + best-effort /stop) and drop the registry entry.

    Called on session delete. Idempotent; the driver_session_id resolution mirrors
    ensure_fanout's (the registry entry's own id wins — it is what was
    actually subscribed).
    """
    entry = _pumps.pop(session_id, None)
    drv = (
        entry.driver_session_id if entry is not None else None
    ) or driver_session_id or session_id
    channel = get_driver_channel()
    try:
        await channel.unsubscribe(drv)
    except Exception:
        logger.warning(
            "chat fan-out: stop unsubscribe failed session=%s", session_id,
            exc_info=True,
        )
    if entry is not None and not entry.task.done():
        # Backstop only — the unsubscribe sentinel is the graceful path; a
        # pump that outlives it (parked on a stuck send) is cancelled rather
        # than leaked.
        entry.task.cancel()


async def _pump(
    session_id: str, project_id: str, owner_id: str,
    queue: asyncio.Queue, detach: Callable[[], None],
) -> None:
    """The single writer per session: forward every listener frame to the
    owner's project-WS sockets, in order, until the channel closes the
    listener (None — bound-breach drop, unsubscribe, channel close)."""
    try:
        # INVARIANT: one writer, listener order, no reordering — a frame is
        # either sent or lost with the listener, never buffered past a peer.
        # Why: a reordered or silently-stalled stream is worse than a gap the
        # browser's assembler refills from the reload projection.
        while True:
            frame = await queue.get()
            if frame is None:
                return
            try:
                await send_to_project_user(project_id, owner_id, {
                    "type": "chat_frame",
                    "session_id": session_id,
                    "frame": frame,
                })
            except Exception:
                # send_to_user already guards per-socket failures; anything
                # raising past it (serialization, a dead session registry)
                # must not kill the pump — the turn's frames keep flowing to
                # the owner's other sockets and the reload path stays whole.
                logger.exception(
                    "chat fan-out: send failed session=%s", session_id)
    finally:
        detach()
        _pumps.pop(session_id, None)
