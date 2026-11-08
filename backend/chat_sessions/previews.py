"""Note-pill preview aggregate — the ONE messages→preview derivation.

# SYSTEM: notes — anchorless system notes pinned to a document (errors and other system messages).

Moved verbatim from routes.chat.sessions_list (plan typed-edges): the typed-edges
read (edges.py) projects note peers and needs the same preview map the sessions
list serves — reuse, not re-derivation. The routes.chat underscore boundary
forbids importing it from there, so the single home is the chat_sessions
package the note shape already lives in (notes_service).
"""
import logging

logger = logging.getLogger(__name__)


# SYSTEM: chat-preview-truncation — single source for preview body truncation.
# Hover popups clip visually at PREVIEW_MAX_HEIGHT (frontend), so this bound only
# guards per-hover/payload transfer size (content is unbounded at creation;
# pathological pastes could otherwise transfer MBs). 2000 covers realistic
# messages while keeping a hard bound. Lived in routes/chat/serializers.py before
# the aggregate moved here (plan typed-edges); re-exported there for its callers.
PREVIEW_CONTENT_MAX = 2000


async def note_previews(db, note_ids: list[str]) -> dict[str, dict]:
    """Derive note pill display from messages, not from the stale
    `title` snapshot. Only note sessions need previews; AI chats read `title`. The
    serializer stays sync and pure — it consumes this map without any DB call.

    # INVARIANT(persisted): messages.chat_id stores the BARE session UUID (schema string),
    # Why: `chat_id IN $ids` matches bare strings, so RecordID-form keys would miss every row.
    # NOT the full RecordID form, so note_ids MUST be extracted to bare ids via
    # extract_id() — str(RecordID) ("chat_sessions:<uuid>") would never match
    # `chat_id IN $ids` and silently empty every preview. The map is keyed by the
    # same bare id; the serializer looks it up by out["session_id"] (also bare).
    #
    # One ordered query, content truncated in SQL (string::slice) so full message
    # bodies — incl. large image data URLs — are never transferred. The scan is
    # bounded to the listed notes' messages (idx_messages_chat_deleted-served).
    # math::min/max is NOT used: it coerces datetimes to floats and returns -inf;
    # ascending ordering + Python first/last-per-chat gives the same result.
    """
    previews_by_session_id: dict[str, dict] = {}
    if not note_ids:
        return previews_by_session_id
    agg = await db.query(
        f"SELECT chat_id, string::slice(content, 0, {PREVIEW_CONTENT_MAX}) AS content, created_at "
        "FROM messages WHERE chat_id IN $ids AND deleted_at IS NONE "
        "ORDER BY created_at ASC",
        {"ids": note_ids},
    )
    by_chat: dict[str, list[dict]] = {}
    for m in (agg or []):
        cid = str(m.get("chat_id", ""))
        if not cid:
            continue
        by_chat.setdefault(cid, []).append(m)
    for cid, msgs in by_chat.items():
        first = msgs[0]
        last = msgs[-1]
        previews_by_session_id[cid] = {
            "first": (first.get("content") or "").strip()[:PREVIEW_CONTENT_MAX] or None,
            "last": (last.get("content") or "").strip()[:PREVIEW_CONTENT_MAX] or None,
            "count": len(msgs),
            "last_at": last.get("created_at"),
        }
    return previews_by_session_id
