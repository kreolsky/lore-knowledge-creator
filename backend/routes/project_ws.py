"""Project-level WebSocket — lightweight notification channel for project tree changes.

No OT, no content sync. Broadcasts project-scoped events (document/reference
lifecycle, project metadata) to all connected project members, plus — for the
harness chat lifecycle — OWNER-FILTERED chat frames (see send_to_user; never
broadcast, plan agent-line-harness-lifecycle step 5).
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

# Event Wiring Table — adding a new event requires touching ALL listed locations:
#
# | Event                  | 1. emit() site         | 2. _SUBSCRIPTIONS below | 3. project-connection.ts dispatch() | 4. useProjectConnection.ts emit() | 5. useReferenceEvents.ts / useEvent() |
# |------------------------|------------------------|-------------------------|-------------------------------------|-----------------------------------|----------------------------------------|
# | document_created       | documents.py           | ✓                       | ✓ case 'document_created'           | emit('project-document-created')  | useEvent('project-document-created')   |
# | document_renamed       | documents.py           | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | document_moved         | documents.py           | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | document_reordered     | documents.py           | ✓                       | ✓ case 'document_reordered'         | emit('project-document-reordered')| useEvent('project-document-reordered') |
# | document_deleted       | documents.py           | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | documents_deleted_batch| documents.py (delete)  | ✓                       | ✓                                   | ✓                                 | ✓ (Sidebar tree drop + useReferenceEvents refs drop) |
# | documents_moved_out    | documents/move.py      | ✓                       | ✓ case 'documents_moved_out'        | emit('project-documents-moved-out') | Sidebar (tree drop + follow-reload) + useReferenceEvents (refs drop) |
# | documents_moved_in     | documents/move.py      | ✓                       | ✓ case 'documents_moved_in'         | emit('project-documents-moved-in')| Sidebar (tree refetch)                |
# | reference_created      | references.py / files  | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | reference_renamed      | references.py          | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | reference_moved        | references.py          | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | reference_deleted      | references.py / docs   | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | reference_updated      | files.py               | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | reference_status_chged | project_ws.py (custom) | — (custom handler)      | ✓                                   | ✓                                 | ✓                                      |
# | content_flushed        | documents.py / refs.py / chat/agent.py / collab/session.py | ✓                       | ✓                                   | ✓                                 | ✓ (useEditorReferenceSync, doc-entry gate) |
# | project_updated        | projects.py            | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | project_deleted        | projects.py            | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | agent_extraction_start | extractor.py           | ✓                       | ✓                                   | ✓                                 | ✓                                      |
# | generate_image_progress| tool_api/image_gen.py  | ✓                       | ✓                                   | ✓                                 | ✓ (MessageList useEvent)               |
# | generate_image_done    | tool_api/image_gen.py  | ✓                       | ✓                                   | ✓                                 | ✓ (MessageList useEvent)               |
# | generate_image_failed  | tool_api/image_gen.py  | ✓                       | ✓                                   | ✓                                 | ✓ (MessageList useEvent)               |
# | chat_frame             | routes/chat/fanout.py (driver-channel listener pump — NOT event_bus; sends via send_to_user) | — (custom sender, owner-filtered — never broadcast) | ✓ case 'chat_frame' (plan agent-line-harness-lifecycle step 7) | — (chat-store dispatch, not useProjectConnection) | chat-store frame reducer via the frame-shared assembler (step 7) |

_SUBSCRIPTIONS: list[tuple[str, list[str] | None]] = [
    ("document_created", ["document_id", "title", "parent_id", "sort_key"]),
    ("document_renamed", ["document_id", "title"]),
    # WHY: document_moved's allowlist MUST carry previous_parent_id /
    # is_reference / title (in addition to the ids). All three are emitted by
    # documents.move.move_document_command, but this list strips every field not named
    # in it — previous previous_parent_id was stripped here, so the frontend's
    # old-host references-panel reload keyed on a value that never arrived, and
    # a kind conversion (move_document node_type) was unactionable by a second
    # client: without is_reference + title it cannot move the node between the
    # tree and a references panel, and keeps a phantom until reload.
    ("document_moved", ["document_id", "parent_id", "sort_key", "previous_parent_id", "is_reference", "title"]),
    ("document_reordered", ["document_id", "parent_id", "sort_key"]),
    ("document_deleted", ["document_id"]),
    ("documents_deleted_batch", ["document_ids", "reference_ids"]),
    # Cross-project subtree move, both ends. The OUT frame rides the SOURCE
    # project's channel and carries the target's id+name so a client whose open
    # doc just left can toast "moved to <project>" and follow via /docs/<id>;
    # the IN frame rides the TARGET's channel and is a plain "refetch the tree".
    ("documents_moved_out", ["document_ids", "reference_ids", "target_project_id", "target_project_name"]),
    ("documents_moved_in", ["document_ids"]),
    ("reference_created", ["reference_id", "title", "document_id", "created_by", "created_by_name"]),
    ("reference_renamed", ["reference_id", "title"]),
    ("reference_moved", ["reference_id", "document_id"]),
    ("reference_deleted", ["reference_id"]),
    ("reference_updated", ["reference_id"]),
    # content_flushed fires on every debounce flush from 4 sites (documents.py,
    # references.py, chat/agent.py, collab/session.py). The frontend listener early-returns
    # unless entity_id is already a `kind:'doc'` transcludeMap entry (chattiness guard).
    # _make_subscriber strips every field NOT in this list → payload is {type, entity_id,
    # entity_type} only; is_reference never arrives (the gate relies on the local map kind).
    ("content_flushed", ["entity_id", "entity_type"]),
    ("project_updated", None),
    ("project_deleted", []),
    ("agent_extraction_started", ["reference_id"]),
    # Live generate_image phase: coarse phase relayed
    # to the chat so it can show progress during the ~10–20s ComfyUI wait. Carries
    # message_id so the frontend can pin the spinner
    # to the generating message AFTER the turn ends (the phase is decoupled from
    # the in-flight streaming object — the detached generation outlives the turn).
    ("generate_image_progress", ["session_id", "phase", "run_id", "message_id"]),
    # Generation is DETACHED — the tool
    # returns {status:"generating"} at once and the result lands asynchronously.
    # These deliver the final chips (refiner + image reference ids) / the failure
    # to the chat over the project-WS so the image survives a doc-switch / turn
    # cancel. The chip is persisted server-side BEFORE the done emit; these frames
    # drive the LIVE update (correlated by session_id + run_id).
    # WHY(wiring-test): keep each _SUBSCRIPTIONS tuple on ONE line. Why:
    # tests/backend/test_event_wiring.py parses this table to prove every emit()
    # has a subscriber; a multi-line entry whose closing line starts with `]`
    # truncates the line-based scan and the NEXT entry reads as unsubscribed
    # (generate_image_failed shipped red this way). The scanner is now bracket-
    # depth aware too, but one-line-per-entry is the readable contract.
    ("generate_image_done", ["session_id", "run_id", "message_id", "reference_ids", "title", "refine", "steps"]),
    ("generate_image_failed", ["session_id", "run_id", "message_id", "error"]),
]


def _make_subscriber(event_type: str, fields: list[str] | None):
    async def handler(**kwargs):
        session = _sessions.get(kwargs.get("project_id"))
        if session:
            msg: dict = {"type": event_type}
            if fields is None:
                msg.update(kwargs)
                msg.pop("project_id", None)
            else:
                for f in fields:
                    if f in kwargs:
                        msg[f] = kwargs[f]
            await session.broadcast(msg)
    return handler


for _event_type, _fields in _SUBSCRIPTIONS:
    _bus_on(_event_type, _make_subscriber(_event_type, _fields))


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


async def _on_reference_status_changed(reference_id: str, project_id: str, status: str, **_kwargs) -> None:
    # DOCX import (and any future async ref processing) emits this directly, unlike
    # transcription which is translated from transcription_complete/error above.
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "reference_status_changed",
            "reference_id": reference_id,
            "status": status,
        })


_bus_on("transcription_complete", _on_transcription_complete)
_bus_on("transcription_error", _on_transcription_error)
_bus_on("reference_status_changed", _on_reference_status_changed)


async def _on_extraction_error(reference_id: str, project_id: str, note_id: str, document_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "extraction_error",
            "reference_id": reference_id,
            "note_id": note_id,
            "document_id": document_id,
        })


_bus_on("extraction_error", _on_extraction_error)


async def _on_agent_error_note(project_id: str, note_id: str, document_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({
            "type": "agent_error_note",
            "note_id": note_id,
            "document_id": document_id,
        })


_bus_on("agent_error_note", _on_agent_error_note)


async def _on_embedding_degraded(project_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({"type": "embedding_degraded"})


async def _on_embedding_recovered(project_id: str, **_kwargs) -> None:
    session = _sessions.get(project_id)
    if session:
        await session.broadcast({"type": "embedding_recovered"})


_bus_on("embedding_degraded", _on_embedding_degraded)
_bus_on("embedding_recovered", _on_embedding_recovered)
