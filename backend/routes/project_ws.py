"""Project-level WebSocket — lightweight notification channel for project tree changes.

No OT, no content sync. Broadcasts project-scoped events (document/reference
lifecycle, project metadata) to all connected project members, plus — for the
harness chat lifecycle — OWNER-FILTERED chat frames (see send_to_user; never
broadcast).
"""
# ARCH: Separate from collab WS — tree metadata + owner-filtered chat frames
# only, no OT, no collaborative content sync.
# Collab WS handles high-frequency content sync; project WS handles low-frequency tree changes.
# ARCH: Subscriber factory + declarative table replaces 13 duplicated handler functions.
# SYSTEM: project-ws — lifecycle broadcasts + owner-filtered chat frames

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from access import get_project_access
from auth import validate_ws_token
from deps import MAX_WS_MESSAGE_SIZE
from event_bus import on as _bus_on

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_TIMEOUT_SEC = 90


# ── Session registry ────────────────────────────────────────────────────────

_sessions: dict[str, ProjectSession] = {}
MAX_PROJECT_SESSIONS = 200


class _WsClient:
    """One connected project-WS socket plus WHO it is.

    Identity lands at accept time (the authenticated user dict) and never
    changes for the socket's lifetime. Broadcast ignores it; the chat
    fan-out's user-filtered send keys on it."""

    def __init__(self, ws: WebSocket, user_id: str):
        self.ws = ws
        self.user_id = user_id


class ProjectSession:
    """In-memory set of WS clients subscribed to a project's notifications."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.clients: dict[int, _WsClient] = {}

    def add(self, ws: WebSocket, user_id: str) -> None:
        self.clients[id(ws)] = _WsClient(ws, user_id)

    def remove(self, ws: WebSocket) -> None:
        self.clients.pop(id(ws), None)

    async def broadcast(self, message: dict) -> None:
        text = json.dumps(message)
        for ws_id, client in list(self.clients.items()):
            try:
                await client.ws.send_text(text)
            except Exception:
                self.clients.pop(ws_id, None)

    # ARCH (plan agent-line-harness-lifecycle step 5): chat frames ride THIS
    # send, never broadcast — AI chats are owner-only
    # (routes/chat/sessions.py _require_session_access), so their delivery
    # set is the session owner's sockets, not every project member. The
    # filter is the same fact the REST gate checks (session.user_id); a
    # socket that dies mid-send is dropped exactly like broadcast's.
    async def send_to_user(self, user_id: str, message: dict) -> None:
        text = json.dumps(message)
        for ws_id, client in list(self.clients.items()):
            if client.user_id != user_id:
                continue
            try:
                await client.ws.send_text(text)
            except Exception:
                self.clients.pop(ws_id, None)


async def send_to_project_user(
    project_id: str, user_id: str, message: dict,
) -> None:
    """User-filtered project-WS send — the chat fan-out's browser hop.

    A no-op when nobody's connected: frames persist through the channel's
    projection and the reload path replays them, so an empty delivery set
    loses nothing (the live tab that reconnected is the reload consumer)."""
    session = _sessions.get(project_id)
    if session is not None:
        await session.send_to_user(user_id, message)


def _get_or_create(project_id: str) -> ProjectSession:
    if project_id not in _sessions:
        _sessions[project_id] = ProjectSession(project_id)
    return _sessions[project_id]


# ── Auth ────────────────────────────────────────────────────────────────────

async def _authenticate(ws: WebSocket, project_id: str) -> dict | None:
    """Validate WS token and project read access. Returns user dict or None.

    ARCH: Auth before accept — validate_ws_token checks token_version against DB.
    """
    user = await validate_ws_token(ws)
    if user is None:
        await ws.close(code=4001, reason="Unauthorized")
        return None
    access = await get_project_access(project_id, user)
    if access is None:
        await ws.close(code=4003, reason="No project access")
        return None
    return user


# ── WebSocket endpoint ──────────────────────────────────────────────────────

@router.websocket("/ws/project/{project_id}")
async def project_ws(ws: WebSocket, project_id: str):
    """Lightweight WS for project-level notifications (tree changes, lifecycle)."""
    # ARCH: Auth before accept — reject unauthenticated connections without allocating resources (H-5).
    user = await _authenticate(ws, project_id)
    if user is None:
        return
    await ws.accept()

    if project_id not in _sessions and len(_sessions) >= MAX_PROJECT_SESSIONS:
        await ws.close(code=4029, reason="Too many sessions")
        return

    session = _get_or_create(project_id)
    session.add(ws, str(user.get("user_id") or ""))
    logger.debug("Project WS connect: project=%s user=%s clients=%d",
                 project_id, user.get("name", ""), len(session.clients))
    await ws.send_text(json.dumps({"type": "init"}))
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=HEARTBEAT_TIMEOUT_SEC)
                if len(raw.encode("utf-8")) > MAX_WS_MESSAGE_SIZE:
                    await ws.close(code=4009, reason="Message too large")
                    break
            except asyncio.TimeoutError:
                await ws.close(code=4008, reason="Heartbeat timeout")
                break
    except WebSocketDisconnect:
        pass  # WHY: expected lifecycle — client disconnect is the normal end of a WS session.
    except Exception:
        logger.exception("Project WS error for %s", project_id)
    finally:
        session.remove(ws)
        if not session.clients:
            _sessions.pop(project_id, None)


# ── Event bus subscribers (declarative) ─────────────────────────────────────

# ARCH: the project WS forwards an event's emitted kwargs verbatim (minus
# project_id); the field names ARE the contract, typed once in
# frontend/src/events/event-types.ts under 'ws:<type>'. tests/backend/
# test_event_wiring.py binds the two sides — a field added at an emit site and
# not typed there fails CI, and nothing is dropped at runtime either way.

# Which bus events reach the project WS — names only, every entry forwarded
# verbatim. transcription_complete / transcription_error stay custom handlers
# below: their wire type is reference_status_changed with a SYNTHESIZED status
# (a translation, not a passthrough); agent_error_note also stays custom —
# nothing emits it yet.
_SUBSCRIPTIONS: tuple[str, ...] = (
    "document_created",
    "document_renamed",
    "document_moved",
    "document_reordered",
    "document_deleted",
    "documents_deleted_batch",
    # Cross-project subtree move, both ends. The OUT frame rides the SOURCE
    # project's channel and carries the target's id+name so a client whose open
    # doc just left can toast "moved to <project>" and follow via /docs/<id>;
    # the IN frame rides the TARGET's channel and is a plain "refetch the tree".
    "documents_moved_out",
    "documents_moved_in",
    "reference_created",
    "reference_renamed",
    "reference_moved",
    "reference_deleted",
    "reference_updated",
    "reference_status_changed",
    # content_flushed fires on every debounce flush from many sites (documents,
    # references, the agent, collab). The frontend listener gates on an existing
    # transcludeMap entry (chattiness guard); is_reference arrives whenever the
    # emitter states it.
    "content_flushed",
    "project_updated",
    "project_deleted",
    "agent_extraction_started",
    "extraction_error",
    "embedding_degraded",
    "embedding_recovered",
)


def _make_subscriber(event_type: str):
    """Forward the emitted kwargs VERBATIM minus project_id (the routing key)."""

    async def handler(**kwargs):
        session = _sessions.get(kwargs.get("project_id"))
        if session:
            msg = dict(kwargs)
            msg.pop("project_id", None)
            msg["type"] = event_type
            await session.broadcast(msg)

    return handler


for _event_type in _SUBSCRIPTIONS:
    _bus_on(_event_type, _make_subscriber(_event_type))


async def _on_transcription_complete(reference_id: str, project_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "reference_status_changed",
            "reference_id": reference_id,
            "status": "ready",
        })


async def _on_transcription_error(reference_id: str, project_id: str) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "reference_status_changed",
            "reference_id": reference_id,
            "status": "error",
        })


_bus_on("transcription_complete", _on_transcription_complete)
_bus_on("transcription_error", _on_transcription_error)


async def _on_agent_error_note(project_id: str, note_id: str, document_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "agent_error_note",
            "note_id": note_id,
            "document_id": document_id,
        })


_bus_on("agent_error_note", _on_agent_error_note)


async def _on_inbox_changed(project_id: str, user_id: str, document_id: str, **_kwargs) -> None:
    """see SYSTEM: inbox — the unread pool of ONE user changed; tell ONLY that user.

    A CUSTOM handler with send_to_project_user, NOT a _SUBSCRIPTIONS entry:
    the declarative table BROADCASTS to every member, and the flag is
    per-recipient — another member must not learn whose mail arrived (the
    serializer hides the raw id for the same reason). user_id is the routing
    key here, never a wire field.
    """
    await send_to_project_user(project_id, user_id, {
        "type": "inbox_changed",
        "document_id": document_id,
    })


_bus_on("inbox_changed", _on_inbox_changed)
