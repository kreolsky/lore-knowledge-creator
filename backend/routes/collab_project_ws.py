"""Persistent project-level collab WebSocket — multiplexed join/leave per entity.

# ARCH: One WS connection per project instead of per-entity. Client sends join/leave
# messages to subscribe to entity sessions. Binary frames carry Yjs sync/awareness
# with entity_id envelope. JSON frames carry control (join/leave/heartbeat/flush).
"""

from __future__ import annotations

import json
import logging
import time

from collab.drop_telemetry import is_mutation, record_dropped_update
from collab.join import (
    SessionCapExceeded,
    join_collab_session,
    leave_collab_session,
)
from collab.session import (
    VALID_ENTITY_TYPES,
    CollabSession,
    ConnectedClient,
)
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from access import get_document_access, get_project_access
from auth import validate_ws_token
from db import fetch_one
from deps import MAX_WS_MESSAGE_SIZE

logger = logging.getLogger(__name__)

router = APIRouter()


async def _authenticate_project_ws(ws: WebSocket, project_id: str) -> tuple[dict, str] | None:
    user = await validate_ws_token(ws)
    if user is None:
        await ws.close(code=4001, reason="Unauthorized")
        return None

    access = await get_project_access(project_id, user)
    if access is None:
        await ws.close(code=4003, reason="No project access")
        return None

    return user, access


async def _handle_join(
    ws: WebSocket,
    project_id: str,
    user: dict,
    access_level: str,
    msg: dict,
    joined: dict[str, tuple[CollabSession, ConnectedClient]],
) -> None:
    entity_type = msg.get("entity_type", "")
    entity_id = msg.get("entity_id", "")
    user_id = user["user_id"]
    user_name = user.get("name", "")

    if entity_type not in VALID_ENTITY_TYPES:
        await ws.send_text(json.dumps({
            "type": "error",
            "entity_id": entity_id,
            "message": "Invalid entity type",
        }))
        return

    entity = await fetch_one("documents", entity_id)
    if not entity:
        await ws.send_text(json.dumps({
            "type": "error",
            "entity_id": entity_id,
            "message": "Entity not found",
        }))
        return

    entity_project_id = entity.get("project_id", "")
    if entity_project_id != project_id:
        await ws.send_text(json.dumps({
            "type": "error",
            "entity_id": entity_id,
            "message": "Entity not found",
        }))
        return

    if entity_id in joined:
        await ws.send_text(json.dumps({
            "type": "error",
            "entity_id": entity_id,
            "message": "Already joined",
        }))
        return

    # WHY: kept as a serial await after the entity fetch — NOT asyncio.gather'd with it.
    # Concurrent RPCs on the single shared SurrealDB connection contend badly (measured
    # ~10-20x SLOWER than serial, ~40ms stalls). Fix 3 of the perf audit was reverted
    # for this reason; do not re-parallelize DB reads on the shared connection.
    doc_access = await get_document_access(entity_id, user)
    effective_access = doc_access if doc_access is not None else access_level

    try:
        session, client = await join_collab_session(
            ws, entity_type, entity_id, entity, user_id, user_name, effective_access,
        )
    except SessionCapExceeded:
        await ws.send_text(json.dumps({
            "type": "error",
            "entity_id": entity_id,
            "message": "Too many sessions",
        }))
        return

    joined[entity_id] = (session, client)

    await ws.send_text(json.dumps({
        "type": "init",
        "entity_id": entity_id,
        "users": session.active_users(),
    }))

    await session.broadcast(
        {"type": "user_joined", "entity_id": entity_id,
         "user": {"user_id": user_id, "name": user_name, "access_level": effective_access}},
        exclude_ws=ws,
    )


async def _handle_leave(
    ws: WebSocket,
    user_id: str,
    msg: dict,
    joined: dict[str, tuple[CollabSession, ConnectedClient]],
) -> None:
    entity_id = msg.get("entity_id", "")
    entry = joined.pop(entity_id, None)
    if entry is None:
        return

    session, _client = entry
    await leave_collab_session(ws, session, user_id, entity_id=entity_id)


async def _route_binary_message(
    ws: WebSocket,
    data: bytes,
    joined: dict[str, tuple[CollabSession, ConnectedClient]],
    *,
    user_id: str,
    project_id: str,
) -> None:
    """Route a binary frame to the correct entity session."""
    from collab.sync import unwrap_binary

    unwrapped = unwrap_binary(data)
    if unwrapped is None:
        return
    entity_id, msg_type, payload = unwrapped

    entry = joined.get(entity_id)
    if entry is None:
        if is_mutation(msg_type, payload):
            await record_dropped_update(
                "server-drop-unjoined", ws_id=id(ws), user_id=user_id,
                project_id=project_id, entity_id=entity_id, message=payload,
            )
        return

    session, _client = entry
    # WHY: handle_binary_message returns silently for a socket the session no longer lists
    # as a client (session.py: `if not sender_client`); recorded here, where the user is known.
    if id(ws) not in session.clients:
        if is_mutation(msg_type, payload):
            await record_dropped_update(
                "server-drop-unregistered", ws_id=id(ws), user_id=user_id,
                project_id=project_id, entity_id=entity_id, message=payload,
            )
    await session.handle_binary_message(data, ws, is_multiplexed=True)


@router.websocket("/ws/collab/project/{project_id}")
async def collab_project_ws(ws: WebSocket, project_id: str):
    auth = await _authenticate_project_ws(ws, project_id)
    if auth is None:
        return

    await ws.accept()

    user, access_level = auth
    user_id = user["user_id"]
    user_name = user.get("name", "")

    joined: dict[str, tuple[CollabSession, ConnectedClient]] = {}

    logger.debug("Project collab WS connect: project=%s user=%s", project_id, user_name)

    try:
        while True:
            raw = await ws.receive()

            # INVARIANT: a "websocket.disconnect" frame must break the loop, not fall  Why: ws.receive() returns the disconnect frame instead of raising WebSocketDisconnect; if the loop doesn't break on it, the next receive() raises and the session tears down uncleanly.
            # through. ws.receive() RETURNS the disconnect message (it does not raise
            # WebSocketDisconnect); ignoring it lets the next receive() raise
            # RuntimeError("Cannot call receive once a disconnect has been received").
            # Why: that surfaced as a spurious ERROR-level traceback on every normal
            # client disconnect in prod.
            if raw["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(raw.get("code", 1000))

            for _session, _client in joined.values():
                _client.last_activity = time.monotonic()

            if "text" in raw:
                text_data = raw["text"]
                if len(text_data) > MAX_WS_MESSAGE_SIZE:
                    await ws.send_text(json.dumps({
                        "type": "error", "message": "Message too large",
                    }))
                    continue

                try:
                    msg = json.loads(text_data)
                except json.JSONDecodeError:
                    try:
                        await ws.send_text(json.dumps({
                            "type": "error", "code": "malformed_json",
                            "message": "Invalid JSON in WS message",
                        }))
                    except Exception:
                        pass  # WHY: best-effort error frame on a dead socket.
                    continue

                msg_type = msg.get("type")

                if msg_type == "join":
                    await _handle_join(ws, project_id, user, access_level, msg, joined)

                elif msg_type == "leave":
                    await _handle_leave(ws, user_id, msg, joined)

                elif msg_type == "heartbeat":
                    # WHY: echo the client-supplied `t` so the editor can measure
                    # per-beat RTT (perf telemetry). The multiplexed channel is
                    # channel-level, not per-entity, so the ack carries no entity_id.
                    # A missing `t` (legacy client) still gets a bare ack. last_activity
                    # is bumped on every recv above — no state mutated here.
                    ack: dict = {"type": "heartbeat_ack"}
                    if "t" in msg:
                        ack["t"] = msg["t"]
                    await ws.send_text(json.dumps(ack))

                elif msg_type == "flush":
                    entity_id = msg.get("entity_id", "")
                    entry = joined.get(entity_id)
                    if entry:
                        session, _client = entry
                        await session._flush_if_needed(force=True)
                        await ws.send_text(json.dumps({"type": "flush_ack", "entity_id": entity_id}))

                elif msg_type == "selection":
                    # Advisory region-lock registry — same handler the
                    # per-entity channel uses. The message carries its own entity_id.
                    from collab.dispatch import handle_selection_message
                    entry_id = msg.get("entity_id", "")
                    entry = joined.get(entry_id)
                    if entry:
                        _session, client = entry
                        handle_selection_message(msg, client, default_entity_id=entry_id)

                else:
                    await ws.send_text(json.dumps({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    }))

            elif "bytes" in raw:
                binary_data = raw["bytes"]
                if len(binary_data) > MAX_WS_MESSAGE_SIZE:
                    continue
                await _route_binary_message(ws, binary_data, joined, user_id=user_id, project_id=project_id)

    except WebSocketDisconnect:
        logger.debug("Project collab WS disconnect: project=%s user=%s", project_id, user_name)
    except Exception:
        logger.exception("Project collab WS error: project=%s user=%s", project_id, user_id)
        try:
            await ws.close(code=1011, reason="Internal error")
        except Exception:
            logger.debug("Failed to close project WS on error", exc_info=True)
    finally:
        for entity_id, (session, _client) in list(joined.items()):
            await leave_collab_session(ws, session, user_id, entity_id=entity_id)
        joined.clear()
