"""Mint the compaction continuation chat row from the typed `compaction` frame.

# SYSTEM: compaction — the compaction chat-row minting half (the transcript
#   halves are driver-side). Compaction is a FRAME EVENT on the turn
#   stream, and the backend mints the row from it — so a turn that dies before
#   the event mints nothing (no half-archived chat), and the driver→backend
#   direction carries Tool-API calls only.

# ARCH: the frame is
# dsh's compaction/* vocabulary end to end — the plugin relays the driver's
# `compaction/end` audit event as the typed frame, and `compaction_entry_id`
# IS dsh's own `compactionId` (stable per compaction, minted by dsh, never
# derived from a checkpoint seq). The module is the Lore-domain mint
# (chat_sessions rows are product state dsh cannot own).
# Idempotent per compaction WINDOW — the source session's
# compactionId identifies the window (a replayed frame mints nothing; a later
# compaction carries a different id).
"""
import logging
from uuid import uuid4

from db import create_record, fetch_one, get_db

logger = logging.getLogger(__name__)

# Context-layer fields inherited from the SOURCE chat_session when minting a
# compaction continuation. `mode` is NOT here and is no longer written anywhere
# — the column stays in the schema unread; the row simply inherits the context
# layer below.
_INHERITABLE_CHAT_FIELDS = (
    "document_id", "context_document_ids", "model",
    "system_prompt_id", "target_doc_id",
)


async def window_leaf(payload: dict, session_id: str) -> str | None:
    """The leaf identifying THIS compaction window: the frame's
    `compaction_entry_id` — dsh's own compactionId (the plugin's map relays it
    from the `compaction/end` audit event).

    # INVARIANT(persisted): the window key is the frame's `compaction_entry_id`.
    # Why: the id is minted BY dsh's compaction and never changes, so a
    # replayed frame keys to the same window and mints nothing new. A frame
    # without it (malformed) falls to None — the source-id guard alone — never
    # a live-leaf read (the turn keeps appending after compaction, so a
    # delayed read answers a DIFFERENT question and mints a duplicate pair).
    """
    del session_id
    return str(payload.get("compaction_entry_id") or "") or None


async def _find_continuation(source_session_id: str, leaf: str | None) -> str | None:
    """Return the id of the continuation chat already minted for THIS
    compaction window (source + leaf), or None — the idempotency guard."""
    db = await get_db()
    leaf_clause = " AND compacted_at_leaf = $leaf" if leaf else ""
    rows = await db.query(
        "SELECT meta::id(id) AS cid FROM chat_sessions "
        f"WHERE compacted_from = $src AND archived = false{leaf_clause} "
        "AND deleted_at IS NONE",
        {"src": source_session_id, "leaf": leaf},
    )
    if rows:
        return rows[0].get("cid")
    return None


async def _create_continuation_chat(
    cont_id: str, source_session_id: str, source_chat: dict, leaf: str | None,
) -> None:
    """Mint the continuation chat_sessions row pinned to the same driver session tree.

    Inherits the document/context/model layer from the SOURCE chat (looked up by
    the same id as the driver session). NOT archived; ready for new turns — it is the
    original session file continuing past a CompactionEntry, NOT a fork.
    """
    chat_data: dict = {
        "project_id": source_chat.get("project_id", ""),
        "user_id": source_chat.get("user_id", ""),
        "title": "Continued session",
        "compacted_from": source_session_id,
        "compacted_at_leaf": leaf,
    }
    for field in _INHERITABLE_CHAT_FIELDS:
        if source_chat.get(field) is not None:
            chat_data[field] = source_chat[field]
    await create_record("chat_sessions", cont_id, chat_data)


async def _mint_continuation_row(
    session_id: str, source_chat: dict, leaf: str | None,
) -> tuple[str, bool]:
    """The continuation row (window-keyed idempotency). Returns (chat_id, created)."""
    existing = await _find_continuation(session_id, leaf)
    if existing is not None:
        return existing, False
    cont_id = str(uuid4())
    await _create_continuation_chat(cont_id, session_id, source_chat, leaf)
    return cont_id, True


def _mint_inputs(payload: dict) -> tuple[str, str] | None:
    """Validate the event's identity fields → (session_id, fork_id), or None."""
    session_id = str(payload.get("session_id") or "")
    fork_id = str(payload.get("fork_id") or "")
    if not session_id or not fork_id:
        logger.warning("compaction event missing session_id/fork_id: %s", payload)
        return None
    return session_id, fork_id


async def _record_compaction_completed(session_id: str, tokens_before) -> None:
    """Telemetry: a compaction completed and the continuation row was minted.
    Fire-and-forget."""
    try:
        from telemetry_store import record_telemetry_events

        await record_telemetry_events([{
            "category": "agent",
            "kind": "compaction_completed",
            "user_id": "",
            "project_id": "",
            "entity_id": session_id,
            "detail": {"tokens_before": tokens_before},
        }])
    except Exception:
        logger.warning("compaction_completed telemetry record failed", exc_info=True)


async def mint_compaction_chats(payload: dict) -> dict:
    """Mint the continuation chat row from ONE `compaction` frame event.

    The event carries the driver-side fork's identity + routing fields
    (fork_id / project_id / user_id / document_id) plus tokens_before. Runs
    AFTER compaction completed (the event is post-compaction), so a turn that
    died before the event mints nothing, and a replayed event mints nothing
    new (window-keyed idempotency).

    # ARCH: the archived freeze row is deleted with the freeze subsystem —
    # the continuation is the only row minted.

    The source chat row must exist (the session had a turn); a missing source
    row mints nothing (nothing to inherit from — logged, not an error).
    """
    inputs = _mint_inputs(payload)
    if inputs is None:
        return {"minted": False, "reason": "missing_fields"}
    session_id, _fork_id = inputs

    source_chat = await fetch_one("chat_sessions", session_id)
    if source_chat is None:
        logger.warning("compaction event for unknown source chat %s", session_id)
        return {"minted": False, "reason": "source_chat_missing"}

    leaf = await window_leaf(payload, session_id)
    continuation_chat_id, created_cont = await _mint_continuation_row(
        session_id, source_chat, leaf,
    )
    if created_cont:
        await _record_compaction_completed(session_id, payload.get("tokens_before"))
    return {
        "continuation_chat_id": continuation_chat_id,
        "created_continuation": created_cont,
    }
