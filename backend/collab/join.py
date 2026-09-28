"""Shared join/leave helpers for the project collab WS channel.

# ARCH: The join sequence is channel-agnostic:
#       cap check → get/create session → add client → start flush.
#       The channel-specific parts (auth, init message format, error format) remain in callers.
# INVARIANT: client removal is single-owner — the session's _reap_zombie_clients()
#            is the sole reaper. The channel delegates removal by calling
#            leave_collab_session(ws, ...) which calls session.remove_client(ws)  Why: a single reaper (_reap_zombie_clients) avoids double-free races on client cleanup; the channel delegates removal to one owner.
#            keyed by id(ws). No parallel timeout runs.
#            Why: parallel reapers can race, leaving dangling client refs or double-removing.
"""

from __future__ import annotations

import logging

from fastapi import WebSocket

from collab.registry import (
    _cleanup_session,
    _get_or_create_session,
    _session_key,
    _sessions,
)
from collab.session import MAX_COLLAB_SESSIONS, CollabSession, ConnectedClient
from jobs import pool as jobs_pool

logger = logging.getLogger(__name__)


class SessionCapExceeded(Exception):
    """Raised when the session count would exceed MAX_COLLAB_SESSIONS."""


async def join_collab_session(
    ws: WebSocket,
    entity_type: str,
    entity_id: str,
    entity: dict,
    user_id: str,
    user_name: str,
    access_level: str,
) -> tuple[CollabSession, ConnectedClient]:
    """Shared join logic: cap check → get/create session → add client → start flush.

    Returns (session, client) on success.
    Raises SessionCapExceeded if the session cap would be exceeded.
    """
    key = _session_key(entity_type, entity_id)
    if key not in _sessions and len(_sessions) >= MAX_COLLAB_SESSIONS:
        raise SessionCapExceeded(
            f"Session cap reached ({MAX_COLLAB_SESSIONS}), cannot create session for {key}"
        )

    content = (entity.get("content") or "") if entity else ""
    session = await _get_or_create_session(entity_type, entity_id, content, entity=entity)
    client = session.add_client(ws, user_id, user_name, access_level)
    session.start_periodic_flush()

    logger.debug("Collab join: %s=%s user=%s access=%s clients=%d",
                 entity_type, entity_id, user_name, access_level, len(session.clients))

    # INVARIANT: safety-open backup only on full-access open.
    # Why: it protects edits-about-to-happen; non-editors have nothing to protect.
    if access_level == "full" and not session.is_reference:
        await _enqueue_open_backup(session)

    return session, client


async def _enqueue_open_backup(session: CollabSession) -> None:
    """Enqueue the safety-on-open backup check (runs in the arq worker)."""
    try:
        from content_hash import hash_content as _compute_hash

        from table_serialize import capture_tables_json
        # WHY: capture tables on the hot path (web process) where session.ydoc lives;
        # the worker forwards the string only (never loads a Y.Doc — plan §D worker rule).  Why: the live ydoc lives in the web-process session; the worker has no Y.Doc, so capture happens on the hot path and the worker only forwards the string.
        content = session.content
        content_hash = _compute_hash(content)
        tables_json = capture_tables_json(session.ydoc)
        await jobs_pool.enqueue(
            "auto_backup_open_task",
            session.entity_id, content, session.is_reference, content_hash, tables_json,
            job_id=f"open:{session.entity_id}:{content_hash}",
        )
    except Exception:
        logger.warning("Failed to enqueue open backup for %s", session.entity_id, exc_info=True)


async def leave_collab_session(
    ws: WebSocket,
    session: CollabSession,
    user_id: str,
    entity_id: str | None = None,
) -> None:
    """Shared leave logic: remove client, broadcast user_left, cleanup if empty.

    INVARIANT: this is the sole non-reaper path for client removal. The session's
    _reap_zombie_clients() handles timed-out clients; this handles explicit disconnect.
    Why: parallel removal risks double-removing or leaving dangling refs.

    entity_id: if provided, included in the broadcast (multiplexed channel format).
    """
    # Capture the leaving client BEFORE removal — it carries user_name/access_level,
    # and self.content (the live doc) must be snapshotted before the client dict
    # changes. Gate the last-session upsert on _has_pushed so viewers/commentators
    # that never edited do not trigger a backup (mirrors the handoff discriminator).
    client = session.remove_client(ws)
    if client is not None and client.user_id in session._has_pushed and not session.is_reference:
        await session._flush_pipeline._enqueue_last_session_backup(client.user_id, client.user_name)
    # Clear the advisory region-lock selection so a leaver's stale
    # selection stops blocking edits immediately. The caller passes the
    # entity_id (multiplexed channel frame).
    leave_entity_id = entity_id or session.entity_id
    try:
        from collab import selection_registry as reg
        reg.clear(leave_entity_id, user_id)
    except Exception:
        logger.debug("selection clear on leave failed for %s", leave_entity_id, exc_info=True)
    msg = {"type": "user_left", "user_id": user_id}
    if entity_id is not None:
        msg["entity_id"] = entity_id
    await session.broadcast(msg)
    if not session.clients:
        await _cleanup_session(session.entity_type, session.entity_id)
