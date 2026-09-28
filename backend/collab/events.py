"""Event bus handlers — apply_external_content_change and collab event routing.

Also home to `merge_live_content` and `broadcast_agent_editing`: the REST→collab
bridge's read-side gate and the agent-editing presence signal.
"""
# SYSTEM: collab-events — event bus handlers for REST→collab bridge

from __future__ import annotations

import asyncio
import json
import logging

from textmatch import surgical_splice_text

from collab import registry
from collab.registry import _session_key, _sessions
from collab.session import CollabSession, ConnectedClient, _safe_ws_close
from collab.sync import (
    MSG_SYNC,
    create_update_message,
    wrap_binary,
)

logger = logging.getLogger(__name__)


def merge_live_content(doc: dict, doc_id: str) -> dict:
    """Return doc with content overridden by the active collab session, if any.

    Single source of truth for the "prefer live Y.Doc content over the stale DB
    record" gate. Why: freshly-pasted content lives in the in-memory Y.Doc until
    the ~1s periodic flush; reading the DB here sends an empty/old body until
    reload. When no live session exists the doc is returned unchanged.
    """
    # Qualified lookup (registry.get_active_session, never a from-import binding):
    # tests patch the OWNING module's name and a frozen binding would make the
    # patch succeed while inert (plan fewer-layers ruling).
    live = registry.get_active_session("doc", doc_id)
    if live is not None:
        return {**doc, "content": live.content}
    return doc


async def broadcast_agent_editing(doc_id: str, *, on_behalf_of: str | None) -> None:
    """Notify a document's live collab session that the agent just edited it.

    Best-effort presence signal: the agent is not a real WS client, so it cannot
    emit Yjs awareness. Instead we broadcast a lightweight `agent_editing` control
    message to the active session (if any) so other editors see a transient
    "agent editing on behalf of <user>" indicator. Failures only log — presence is
    decorative and must never fail an apply.
    """
    session = registry.get_active_session("doc", doc_id)
    if session is None:
        return
    try:
        await session.broadcast({"type": "agent_editing", "entity_id": doc_id,
                                 "on_behalf_of": on_behalf_of})
    except Exception:
        logger.debug("agent_editing broadcast failed for %s", doc_id, exc_info=True)


async def apply_external_content_change(
    entity_type: str, entity_id: str, new_content: str | None = None,
    *, already_persisted: bool = False, tables_json: str | None = None,
    from_cp: int | None = None, to_cp: int | None = None, new_text: str | None = None,
    original_text: str | None = None,
) -> bool:
    """Apply a content change from REST to an active collab session.

    Two mutation modes:

    **Wholesale** (default — `new_content` supplied): delete all + insert as one
    transaction. Used by restore/checkpoint. CRDT merges always converge — no
    ConflictError. Destroys every Yjs RelativePosition anchor (the whole text is
    deleted and re-created), so it MUST NOT be used for agent edits under a pinned
    region (see surgical mode).

    **Surgical** (`from_cp`/`to_cp`/`new_text` supplied): delete the code-point slice
    [from_cp:to_cp) and insert `new_text` at from_cp. Used by the agent edit path
. Preserves Yjs RelativePosition anchors in the
    unchanged prefix/suffix so the frontend's pinned-region anchor survives the
    agent's own edits and the user's edits around it.

    # ARCH (B1 RISK — verified): pycrdt `Text` indexes in UTF-8 BYTES, not code
    # points. The wholesale `del text[0:len(text)]` was accidentally unit-agnostic;
    # the surgical `del text[fb:tb]` is NOT, so from_cp/to_cp (code points from
    # `resolve_edit_range`) are converted to UTF-8 byte offsets via the shared
    # `surgical_splice_text` helper, re-reading the live text inside `_write_lock`.

    # ARCH: TOCTOU corruption guard — `original_text` (the proposal
    # resolver's expected content of `[from_cp:to_cp)`) is verified against the live
    # slice re-read inside `_write_lock` AFTER the two awaits between resolve and
    # write (`_create_agent_pre_edit_checkpoint` + `_mark_proposal_applied`). The WS
    # editor sync path takes ONLY `_write_lock`, NOT `doc_edit_lock`, so a concurrent
    # editor edit can shift the text during those awaits; without this guard the
    # frozen cp offsets would be converted to bytes against the SHIFTED text and
    # delete the wrong range (corruption). On mismatch, the helper raises
    # AppliedUnverifiedError (fail-stop). None (default) skips the guard.

    tables_json: when not None (wholesale only), rebuild the ``tables`` Yjs subtree
    on the live session ydoc from the captured JSON (see
    table_serialize.apply_tables_json) so a restore rebuilds editable table blocks,
    not just anchor text. None = legacy restore: leave the tables map untouched.

    Returns True if session existed and was updated.
    """
    session = _sessions.get(_session_key(entity_type, entity_id))
    if not session:
        return False

    surgical = from_cp is not None
    resync_msg = None
    update = None
    async with session._write_lock:
        text = session._get_text()

        if surgical:
            # B1 surgical edit: the cp→byte del+insert + the
            # original_text TOCTOU guard live in the shared `surgical_splice_text`
            # helper (textmatch) so the live and no-session paths share ONE
            # primitive. Re-reads the live text inside `_write_lock`.
            surgical_splice_text(
                text, from_cp, to_cp, new_text, original_text,
            )
        else:
            # INVARIANT(corruption): apply_tables_json MUST run inside _write_lock and BEFORE
            # session.ydoc.get_update(). Why: get_update() snapshots the Y.Doc for the
            # broadcast; running the table rebuild after it omits the tables from the
            # broadcast update and other replicas diverge (orphan anchors). Holding the
            # write lock prevents a concurrent binary update from interleaving between the
            # content replace and the table rebuild.
            from table_serialize import apply_tables_json

            current = str(text)  # wholesale needs the full text (no-op compare + replace)
            content_same = current == new_content
            if content_same and tables_json is None:
                if already_persisted:
                    session._dirty = False
                    session._last_flushed_content = new_content
                return True

            old_len = len(text)
            if old_len > 0:
                del text[0:old_len]
            if new_content:
                text += new_content

            if tables_json is not None:
                apply_tables_json(session.ydoc, tables_json)

            if already_persisted:
                session._dirty = False
                session._last_flushed_content = new_content
                # Mirror the baseline-tables cache so a post-restore destructive edit backs
                # up the RESTORED table state, not a stale one.
                from table_serialize import capture_tables_json

                session._last_flushed_tables_json = (
                    tables_json if tables_json is not None else capture_tables_json(session.ydoc)
                )
                # WHY: re-arm the per-user handoff gate on external content change
                # (restore / agent edit) so the next real editor snapshots a fresh
                # baseline. Why: the doc baseline changed underneath the session, so a
                # prior editor's "already pushed" mark no longer reflects current content.
                # last_editor_id is left as-is — it still reflects the DB row.
                session._has_pushed = {}

        update = session.ydoc.get_update()
        # ARCH: surgical edits are real mutations → always dirty. A
        # wholesale already_persisted replace (transcription import / restore) is a
        # pre-persisted content set → NOT dirty, so the wholesale `already_persisted`
        # reset above wins. The unconditional `session._dirty = True` that used to
        # live here overrode that reset and left pre-persisted imports dirty →
        # redundant flush + spurious content_flushed (files.py transcription import).
        session._dirty = not (not surgical and already_persisted)

        update_msg = create_update_message(update)
        wrapped = wrap_binary(entity_id, MSG_SYNC, update_msg)

        resync_msg = wrapped

    if resync_msg is not None:
        await session.broadcast_binary(resync_msg)
    # Fan the REST change out to live sessions on OTHER replicas too — broadcast_binary
    # above only reaches THIS process's clients. Without this, a checkpoint restore or
    # agent edit was invisible to other replicas until reload. See publish_doc_update.
    from ydoc_store import publish_doc_update

    await publish_doc_update(entity_id, update)
    # Track this external-content append for the flush-time compaction gate.
    if session is not None:
        session._updates_since_compact += 1
    return True


async def _on_note_event(entity_type: str, entity_id: str, event: dict) -> None:
    session = _sessions.get(_session_key(entity_type, entity_id))
    if session:
        # INVARIANT: every multiplexed JSON control frame must carry entity_id —
        # the project WS client (YjsProjectProvider._dispatchJson) routes by it and
        # silently drops frames without it (no toast, no live refresh).  Why: the project WS client routes JSON frames by entity_id and drops any without it; omitting entity_id means no toast and no live refresh — a silent failure. Mirror the
        # user_joined frame in collab_project_ws.py. Why: cross-process events (e.g.
        # worker auto-backup checkpoint_created) reach the client only via this path.
        await session.broadcast({**event, "entity_id": entity_id})


async def _on_entity_deleted(entity_type: str, entity_id: str) -> None:
    session = _sessions.get(_session_key(entity_type, entity_id))
    if session:
        await session.broadcast({"type": "doc_deleted", "entity_id": entity_id})


async def _on_documents_deleted_batch(document_ids: list[str], **_kwargs) -> None:
    """Batch delete — close open collab sessions for every document in the set.

    Replaces per-document entity_deleted emissions in the batch-delete path.
    """
    for doc_id in document_ids:
        session = _sessions.get(_session_key("doc", doc_id))
        if session:
            await session.broadcast({"type": "doc_deleted", "entity_id": doc_id})


async def _close_after_revocation(ws, session: CollabSession) -> None:
    await asyncio.sleep(1.0)
    await _safe_ws_close(ws, code=4003, reason="Access revoked")
    session.clients.pop(id(ws), None)


async def _notify_user_access(session: CollabSession, client: ConnectedClient, access: str | None) -> None:
    msg_type = "access_revoked" if access is None else "access_changed"
    msg = {"type": msg_type}
    if access is not None:
        msg["level"] = access
    try:
        await client.ws.send_text(json.dumps(msg))
    except Exception as exc:
        logger.warning("WS send failed for user=%s entity=%s: %s", client.user_id, session.entity_id, exc)
        await _safe_ws_close(client.ws, code=1011)
        session.clients.pop(id(client.ws), None)
        return
    if access is None:
        asyncio.create_task(_close_after_revocation(client.ws, session))


async def _on_access_changed(project_id: str, user_id: str, access: str | None) -> None:
    for session in list(_sessions.values()):
        for client in list(session.clients.values()):
            if client.user_id == user_id:
                await _notify_user_access(session, client, access)


async def _on_backlinks_changed_batch(document_ids: list[str]) -> None:
    for doc_id in document_ids:
        session = _sessions.get(_session_key("doc", doc_id))
        if session:
            await session.broadcast({"type": "backlinks_changed", "entity_id": doc_id})


_subscribed = False


def subscribe_events() -> None:
    global _subscribed
    if _subscribed:
        return
    _subscribed = True
    from event_bus import on as _bus_on
    _bus_on("entity_deleted", _on_entity_deleted)
    _bus_on("documents_deleted_batch", _on_documents_deleted_batch)
    _bus_on("access_changed", _on_access_changed)
    _bus_on("backlinks_changed_batch", _on_backlinks_changed_batch)
    _bus_on("checkpoint_created", _on_note_event)
    _bus_on("document_history_added", _on_note_event)
    # ARCH: these three complete the NotesPanel
    # realtime contract (note_session_created / _deleted / _message_changed).
    # _on_note_event is generic — broadcasts {**event, entity_id} to the doc session.
    _bus_on("note_session_created", _on_note_event)
    _bus_on("note_session_deleted", _on_note_event)
    _bus_on("note_message_changed", _on_note_event)
