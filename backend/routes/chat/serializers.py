"""Serialization helpers and validators for the chat subsystem."""
# SYSTEM: chat-serializers — session/message serialization, ref-map, image URL validation
# The session wire shape and the ref map live in `chat_sessions.serialize`.

import logging
import re

from fastapi import HTTPException
from pipeline.extractor.runner import PIPELINE_AUTHOR_ID

from db import extract_id, serialize_record, validate_record_id
from models import resolve_message_author

logger = logging.getLogger(__name__)


# SYSTEM: chat-preview-truncation — single source for preview body truncation.
# Hover popups clip visually at PREVIEW_MAX_HEIGHT (frontend), so this bound only
# guards per-hover/payload transfer size (content is unbounded at creation;
# pathological pastes could otherwise transfer MBs). 2000 covers realistic
# messages while keeping a hard bound.
PREVIEW_CONTENT_MAX = 2000


# WHY: single-session preview used ONLY by the realtime message-mutation emit path
# This is a SEPARATE helper from the grouped
# aggregate in list_sessions: that route keeps its single batched query over all
# listed notes' messages (calling this per-session there would be an N+1 regression).
# Same output shape {first,last,count,last_at} so the serialized card matches the
# list path exactly.
async def _session_preview(db, session_id: str) -> dict:
    """Derive {first,last,count,last_at} preview for a single note session.

    Mirrors the per-chat logic of the list_sessions grouped aggregate, but uses
    bounded LIMIT queries (no full-row transfer) so a long note thread does not
    materialize every message on every mutation. Three cheap indexed queries:
    count + first (ASC LIMIT 1) + last (DESC LIMIT 1).
    """
    count_rows = await db.query(
        "SELECT count() AS c FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE GROUP ALL",
        {"cid": session_id},
    ) or []
    count = int((count_rows[0] or {}).get("c") or 0) if count_rows else 0
    if count == 0:
        return {"first": None, "last": None, "count": 0, "last_at": None}
    first_row = await db.query(
        f"SELECT string::slice(content, 0, {PREVIEW_CONTENT_MAX}) AS content, created_at "
        "FROM messages WHERE chat_id = $cid AND deleted_at IS NONE "
        "ORDER BY created_at ASC LIMIT 1",
        {"cid": session_id},
    )
    last_row = await db.query(
        f"SELECT string::slice(content, 0, {PREVIEW_CONTENT_MAX}) AS content, created_at "
        "FROM messages WHERE chat_id = $cid AND deleted_at IS NONE "
        "ORDER BY created_at DESC LIMIT 1",
        {"cid": session_id},
    )
    first = (first_row[0] if first_row else {}) or {}
    last = (last_row[0] if last_row else {}) or {}
    return {
        "first": (first.get("content") or "").strip()[:PREVIEW_CONTENT_MAX] or None,
        "last": (last.get("content") or "").strip()[:PREVIEW_CONTENT_MAX] or None,
        "count": count,
        "last_at": last.get("created_at"),
    }


# C-5: Only data URIs and HTTPS URLs are allowed for images sent to LLM API.
_DATA_URI_RE = re.compile(r"^data:image/[a-z+]+;base64,", re.IGNORECASE)


def _validate_image_url(url: str) -> None:
    """Reject non-HTTPS / non-data-URI image URLs to prevent SSRF."""
    if _DATA_URI_RE.match(url):
        return
    if url.startswith("https://"):
        return
    raise HTTPException(status_code=400, detail="Image URLs must be data: URIs or HTTPS")


def _serialize_message(
    row: dict,
    session: dict | None = None,
    author_names: dict[str, str] | None = None,
) -> dict:
    """Serialize a message row. When session is provided, resolves and stamps
    author_name using author_names map (fall back to session owner via
    resolve_message_author). PIPELINE_AUTHOR_ID → "system notes" sentinel.
    """
    out = serialize_record(row, "message_id")
    if session is None:
        # never serialized to the frontend (parent_id + tree.ts drive rendering).
        return out
    effective_id = resolve_message_author(row, session)
    if effective_id == PIPELINE_AUTHOR_ID:
        out["author_name"] = "system notes"
    elif author_names:
        name = author_names.get(effective_id)
        if name:
            out["author_name"] = name
    return out


async def _resolve_author_names(db, session: dict, rows: list[dict]) -> dict[str, str]:
    """Build author_id → user_name map for a batch of messages.

    Skips PIPELINE_AUTHOR_ID (serializer maps it to "system notes" directly).
    """
    author_ids: set[str] = set()
    for r in rows:
        aid = resolve_message_author(r, session)
        if aid and aid != PIPELINE_AUTHOR_ID:
            author_ids.add(aid)
    if not author_ids:
        return {}
    placeholders = ", ".join(
        f"type::record('users', '{validate_record_id(aid)}')" for aid in author_ids
    )
    users = await db.query(
        f"SELECT id, name FROM users WHERE id IN [{placeholders}]"
    )
    return {extract_id(u["id"]): u.get("name", "") for u in (users or [])}
