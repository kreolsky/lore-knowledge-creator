"""Telemetry for a client Yjs mutation the server discards without applying it.

WHY: the two discard sites (a frame for an entity the socket has not joined, a frame from
a socket the session does not list as a client) return silently, so a user whose typing
never reached the server leaves no trace of which one ate it. One row per
(socket, entity, kind) makes that queryable without letting a stuck client flood the table.
"""

from __future__ import annotations

import logging

from collab.sync import MSG_SYNC, MSG_SYNC_STEP2, MSG_SYNC_UPDATE
from telemetry_store import record_telemetry_events

logger = logging.getLogger(__name__)

# WHY: bounded so a long-lived process cannot grow it without limit; clearing it only
# lets an already-reported (socket, entity, kind) report once more.
_REPORTED_CAP = 10_000
_reported: set[tuple[int, str, str]] = set()


def is_mutation(msg_type: int, message: bytes) -> bool:
    """True for a sync STEP2/UPDATE — the frames that carry edits. STEP1 and awareness
    are read/presence traffic and are not losses when dropped."""
    return msg_type == MSG_SYNC and len(message) >= 2 and message[1] in (MSG_SYNC_STEP2, MSG_SYNC_UPDATE)


async def record_dropped_update(
    kind: str, *, ws_id: int, user_id: str, project_id: str | None, entity_id: str, message: bytes,
) -> None:
    """Record the first discarded mutation for this (socket, entity, kind). Never raises."""
    key = (ws_id, entity_id, kind)
    if key in _reported:
        return
    if len(_reported) >= _REPORTED_CAP:
        _reported.clear()
    _reported.add(key)
    logger.warning("collab %s: user=%s entity=%s bytes=%d", kind, user_id, entity_id, len(message))
    try:
        await record_telemetry_events([{
            "category": "collab",
            "kind": kind,
            "user_id": user_id,
            "project_id": project_id or None,
            "entity_id": entity_id,
            "detail": {"sync_type": message[1], "bytes": len(message)},
            "client_ts": None,
        }])
    except Exception:
        # WHY: telemetry is append-only best-effort (telemetry_store INVARIANT) — a failed
        # insert must never break the WS receive loop that called it.
        logger.warning("Failed to record %s telemetry for %s", kind, entity_id, exc_info=True)
