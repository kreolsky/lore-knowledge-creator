"""Public share change-ping WebSocket — one anonymous socket per viewed document.

The public page's liveness channel: the server sends {"type":"changed"} the
moment a note or the text of THAT document changes, and the client answers with
the same refetch the old 5 s poll ran. A ping, never a payload — the ONLY
anonymous projection of notes is the REST list in routes/public_notes.py, and
forwarding member-shaped frames would author a second one beside it.

# ARCH: the first anonymous socket in the tree — see SYSTEM: public-share (the
# anonymous surface this channel belongs to). Shape mirrors routes/project_ws.py
# (init frame, heartbeat timeout, message-size cap, per-doc registry); access
# differs fundamentally: project_ws authenticates a user BEFORE accept, this
# route re-proves the SHARE funnel before accept AND before every ping.

# ARCH(accept-then-refuse): unlike project_ws (auth before accept — it protects
# resources an authenticated session allocates), this route calls accept()
# FIRST, then refuses. Why: a close before accept reaches a real browser as an
# HTTP handshake failure — onclose sees 1006, never the code — so the client
# reads a refusal as a network blip and reconnects until give-up. An anonymous
# ping socket allocates nothing before registration, so the accept costs one
# frame and makes every refusal browser-visible (4003/4004/4029/4009).

# INVARIANT(security): access is re-proved through the SAME funnel as every
# other anonymous read. Why: a ping is an anonymous read of "this document
# changed" — it must die the moment the share does, not leak change-signals for
# an unshared doc. The funnel is _require_doc (resolve_share + doc ∈ doc_ids,
# the single funnel of routes/public_share.py) at connect AND before each
# changed ping; on failure the socket is closed 4004 instead of sent.
# Re-proving at ping time does NOT catch revocations after which nothing pings
# (share deleted, doc deleted/moved out, project deleted) — those are the
# revalidation list below. Cost: one resolve_share per ping per socket;
# acceptable because Lore serves a small trusted group, not public SaaS, and
# this surface deliberately carries no rate limit (ARCH(no-rate-limit),
# routes/public_share.py).

No proxy work: prod nginx forwards the whole /ws/ prefix (frontend/nginx.conf),
dev vite likewise (frontend/vite.config.ts) — /ws/public/doc/{id} rides both.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from deps import MAX_WS_MESSAGE_SIZE
from event_bus import on as _bus_on
from routes.public_share import _require_doc, clear_doc_ids_cache

logger = logging.getLogger(__name__)

router = APIRouter()

HEARTBEAT_TIMEOUT_SEC = 90
# Caps: a global and a per-doc ceiling on anonymous sockets. 4029 is
# deliberately NOT in the frontend AUTH_CLOSE_CODES (ws-close-codes.ts): a full
# doc is transient, and the client's own reconnect backoff is the retry.
MAX_PUBLIC_SOCKETS = 500
MAX_PUBLIC_SOCKETS_PER_DOC = 50

_CHANGED_FRAME = json.dumps({"type": "changed"})


# ─── Registry ───────────────────────────────────────────────────────────────

class _PublicClient:
    """One connected anonymous socket plus WHAT it may watch.

    doc_id/project_id land at connect time (from the share context) and never
    change: the ping path re-resolves the share per send anyway, and the
    stored project_id is the revalidation filter (events carry project_id, not
    doc sets)."""

    __slots__ = ("ws", "doc_id", "project_id")

    def __init__(self, ws: WebSocket, doc_id: str, project_id: str):
        self.ws = ws
        self.doc_id = doc_id
        self.project_id = project_id


_registry: dict[str, dict[int, _PublicClient]] = {}


def _drop(client: _PublicClient) -> None:
    """Synchronous registry cleanup — the ONLY cleanup the socket loop owes.

    WHY synchronous: Starlette's TestClient never runs async DB work in a
    finally after disconnect (lessons/2026-03-18-realtime-collab-retro.md,
    Урок 5) — a registry pop is all that must happen synchronously.
    """
    per_doc = _registry.get(client.doc_id)
    if per_doc is not None:
        per_doc.pop(id(client.ws), None)
        if not per_doc:
            _registry.pop(client.doc_id, None)


def _total_sockets() -> int:
    return sum(len(v) for v in _registry.values())


async def _close(client: _PublicClient, code: int, reason: str) -> None:
    """Best-effort close: a dead pipe must not mask the caller's own path."""
    try:
        await client.ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 — the close is courtesy, not contract
        logger.debug("public ws close(%d) failed for doc %s", code, client.doc_id, exc_info=True)


async def _recheck(client: _PublicClient) -> bool:
    """Re-prove the share funnel for one socket; close and drop on failure.

    Returns True when the socket may keep receiving pings. The HTTPException
    from _require_doc (uniform 404 — no existence oracle for anonymous callers)
    maps to close 4004; a share that still resolves but lost allow_comments
    closes 4003 (the channel exists for notes); anything else is a server-side
    fault → 1011.
    """
    try:
        ctx = await _require_doc(client.doc_id)
    except HTTPException:
        await _close(client, 4004, "Not found")
        _drop(client)
        return False
    except Exception:
        logger.exception("public ws access recheck failed for doc %s", client.doc_id)
        await _close(client, 1011, "Internal error")
        _drop(client)
        return False
    if not ctx["allow_comments"]:
        await _close(client, 4003, "Comments off")
        _drop(client)
        return False
    return True


# ─── Trigger events → {"type":"changed"} ─────────────────────────────────────
#
# All four carry the document id as entity_id (note_message_changed's is
# resolved by broadcast_doc_id, chat_sessions/note_events.py). content_flushed
# covers session-less writes too: every set_content(persist=True) caller emits
# it at its own site (REST PATCH, version restore, agent edits, worker jobs).
# No new text event. Handler signatures differ across the four (note events are
# (entity_type, entity_id, event); content_flushed adds project_id,
# is_reference), so the handler takes entity_id plus **_kwargs — a REQUIRED
# parameter an event lacks raises TypeError, which event_bus._invoke_subscriber
# swallows into a counter, and the ping would silently never fire.

async def _on_doc_changed(entity_id: str, **_kwargs) -> None:
    per_doc = _registry.get(entity_id)
    if not per_doc:
        return
    for client in list(per_doc.values()):
        # Each socket's re-check and send runs in its own try/except (log +
        # close 1011): one socket's DB error or dead pipe must not stop the
        # pings to the others on the same document.
        try:
            if not await _recheck(client):
                continue
            await client.ws.send_text(_CHANGED_FRAME)
        except Exception:
            logger.warning("public ws ping failed for doc %s", entity_id, exc_info=True)
            await _close(client, 1011, "Internal error")
            _drop(client)


# ─── Revalidation events — revocations after which nothing pings ─────────────
#
# Today's 5 s poll noticed each of these; the socket must too or the guest page
# goes stale silently. On any of them: clear the doc_ids TTL cache (a move out
# of a subtree share within the TTL would otherwise re-read the OLD doc_ids and
# keep the socket open with nothing re-checking it after the TTL lapses), then
# re-run _require_doc for every socket whose stored project_id matches. Not
# documents_moved_in: arriving under a share grants, never revokes. These
# events carry project_id (+ document_ids etc.) and NO entity_id — hence the
# (project_id, **_kwargs) signature.

async def _on_revalidate(project_id: str, **_kwargs) -> None:
    clear_doc_ids_cache()
    for per_doc in list(_registry.values()):
        for client in list(per_doc.values()):
            if client.project_id != project_id:
                continue
            try:
                await _recheck(client)
            except Exception:
                # Belt-and-braces: _recheck owns its failure paths; this keeps
                # one pathological client from aborting the sweep.
                logger.exception("public ws revalidation failed for doc %s", client.doc_id)
                _drop(client)


# Subscribed with literal _bus_on("<name>", …) at MODULE level: main.py imports
# this module at router registration, and subscribe_backplane_events()
# (backend/main.py lifespan) subscribes only event types that already have a
# subscriber — a deferred import would silently drop cross-process pings (a
# worker-side write must reach a guest connected to any web process). The
# literal form is also what tests/backend/test_event_wiring.py greps.
_bus_on("note_session_created", _on_doc_changed)
_bus_on("note_session_deleted", _on_doc_changed)
_bus_on("note_message_changed", _on_doc_changed)
_bus_on("content_flushed", _on_doc_changed)
_bus_on("share_changed", _on_revalidate)
_bus_on("document_deleted", _on_revalidate)
_bus_on("documents_deleted_batch", _on_revalidate)
_bus_on("document_moved", _on_revalidate)
_bus_on("documents_moved_out", _on_revalidate)
_bus_on("project_deleted", _on_revalidate)


# ─── WebSocket endpoint ──────────────────────────────────────────────────────

@router.websocket("/ws/public/doc/{document_id}")
async def public_doc_ws(ws: WebSocket, document_id: str):
    """Anonymous change-ping socket for ONE shared document (comments-on only)."""
    # WHY accept first: see ARCH(accept-then-refuse) in the module docstring —
    # the refusal codes must be browser-visible, and a pre-accept close is a
    # 1006 handshake failure in every real browser.
    await ws.accept()
    try:
        ctx = await _require_doc(document_id)
    except HTTPException:
        await ws.close(code=4004, reason="Not found")
        return
    except Exception:
        logger.exception("public ws access check failed for %s", document_id)
        await ws.close(code=1011, reason="Internal error")
        return
    if not ctx["allow_comments"]:
        # The channel exists for notes: a comments-off share has nothing to
        # ping (the page opens the socket only when comments are on — same
        # gate as the old poll).
        await ws.close(code=4003, reason="Comments off")
        return
    per_doc = _registry.setdefault(document_id, {})
    if len(per_doc) >= MAX_PUBLIC_SOCKETS_PER_DOC or _total_sockets() >= MAX_PUBLIC_SOCKETS:
        if not per_doc:
            _registry.pop(document_id, None)
        await ws.close(code=4029, reason="Too many sockets")
        return
    client = _PublicClient(ws, document_id, str(ctx["project_id"]))
    per_doc[id(ws)] = client
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
    finally:
        _drop(client)
