"""Shared WebSocket plumbing for the notification sockets.

Two homes: the auth-before-accept project prologue (project_ws +
collab_project_ws) and the passive receive loop (project_ws + public_ws). The ACCEPT-ORDER difference is
deliberately NOT here: project_ws/collab_project_ws authenticate BEFORE
accept, public_ws accepts first then refuses (its ARCH(accept-then-refuse)
stays in that endpoint) — only the code both sides share byte-for-byte moved.

The multiplexed collab socket's own loop is NOT a member: it routes
join/leave/binary frames (ws.receive()), it does not passively drain.
"""
from __future__ import annotations

import asyncio

from fastapi import WebSocket

from access import get_project_access
from auth import validate_ws_token
from deps import MAX_WS_MESSAGE_SIZE

HEARTBEAT_TIMEOUT_SEC = 90


async def authenticate_project_ws(ws: WebSocket, project_id: str) -> tuple[dict, str] | None:
    """Auth-before-accept prologue: WS token, then project read access.

    ARCH: auth before accept — validate_ws_token checks token_version against
    DB; an unauthenticated or unauthorized connection is closed (4001/4003)
    without allocating resources (H-5). Returns (user, access_level) so the
    collab socket can seed per-entity access; the notification socket ignores
    the level. Divergence the two copies carried, named: collab_project_ws
    returned the access level too — the home always does.
    """
    user = await validate_ws_token(ws)
    if user is None:
        await ws.close(code=4001, reason="Unauthorized")
        return None
    access = await get_project_access(project_id, user)
    if access is None:
        await ws.close(code=4003, reason="No project access")
        return None
    return user, access


async def receive_loop(ws: WebSocket, *, timeout: float = HEARTBEAT_TIMEOUT_SEC) -> None:
    """Passively drain a notification socket until disconnect, stall, or oversize.

    Closes 4008 on heartbeat timeout and 4009 on an oversized frame, then
    RETURNS — cleanup stays with the endpoint's finally. WebSocketDisconnect
    propagates: each endpoint keeps its own except arms (project_ws logs
    unexpected errors with project context; public_ws has no such arm and lets
    them surface). The peer is expected to only heartbeat; payloads are read
    for the size cap and discarded.
    """
    while True:
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=timeout)
            if len(raw.encode("utf-8")) > MAX_WS_MESSAGE_SIZE:
                await ws.close(code=4009, reason="Message too large")
                return
        except asyncio.TimeoutError:
            await ws.close(code=4008, reason="Heartbeat timeout")
            return
