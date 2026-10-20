"""Public note routes — comments on a public link, left by anonymous guests.

# ARCH: the ONLY anonymous write surface. Every handler enters through
# `_require_doc` (SYSTEM: public-share's single funnel) and every write through
# access.can_write_note with an anonymous principal, so a share without
# `allow_comments` stays read-only. Writes go through the SAME services as the
# authed note path — create_session_command, insert_user_message,
# delete_note_message, delete_session_command — because the members' live frames
# are emitted inside them; a direct DB write here would leave members' panels
# silent while every REST test stays green.

Identity: the httpOnly `lore_anon` cookie (path /api/public), minted by the FIRST
successful write — never by a read, so a reader who never comments is not tagged.
The author id is `anon_` + a hash of it (chat_sessions.authors); it is never a
users row. Offsets on this surface are UTF-16 both ways: the public editor's
document IS the raw Yjs text, so no code-point conversion happens on the client.
"""
from __future__ import annotations

import hashlib
import logging
import secrets

from chat_sessions.authors import ANON_AUTHOR_PREFIX, author_display, user_names
from chat_sessions.create import create_session_command
from chat_sessions.delete import delete_session_command
from fastapi import APIRouter, HTTPException, Request, Response
from inbox import KIND_NOTE, mark_arrived
from note_anchors import (
    AnchorSpace,
    anchor_space,
    build_anchor,
    relpos_to_wire,
    resolve_anchor,
    utf16_to_cp,
)
from pydantic import BaseModel, field_validator, model_validator

from access import can_write_note
from auth import _browser_scheme_is_https
from db import SAFE_ID_RE, extract_id, fetch_one, get_db, serialize_record
from models import SessionCreate, resolve_message_author
from routes.chat.messages import delete_note_message, insert_user_message
from routes.public_share import _iso_or_none, _require_doc, content_sha256

router = APIRouter()
logger = logging.getLogger(__name__)

ANON_COOKIE = "lore_anon"
_ANON_COOKIE_MAX_AGE = 60 * 60 * 24 * 365
AUTHOR_NAME_MAX = 40
# INVARIANT(security): a guest's message is at most CONTENT_MAX characters.
# Why: "иначе это уже и не заметка вовсе" — and every open public page re-reads the
# whole list every few seconds, so one unbounded guest write would be downloaded by
# every reader and every member panel over and over.
CONTENT_MAX = 2000


class _AuthoredBody(BaseModel):
    author_name: str
    content: str

    @field_validator("author_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= AUTHOR_NAME_MAX:
            raise ValueError(f"author_name must be 1..{AUTHOR_NAME_MAX} characters")
        return value

    @field_validator("content")
    @classmethod
    def _content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be empty")
        if len(value) > CONTENT_MAX:
            raise ValueError(f"content must be at most {CONTENT_MAX} characters")
        return value


class PublicNoteCreate(_AuthoredBody):
    # UTF-16 range over the public payload's `content`, plus the payload's
    # `content_sha256` echoed back; all three or none (an anchorless note on the
    # whole document).
    start: int | None = None
    end: int | None = None
    content_sha256: str | None = None

    @model_validator(mode="after")
    def _anchor_complete(self) -> "PublicNoteCreate":
        given = [v is not None for v in (self.start, self.end, self.content_sha256)]
        if any(given) and not all(given):
            raise ValueError("start, end and content_sha256 must be sent together")
        return self


class PublicReply(_AuthoredBody):
    parent_id: str | None = None


# ─── Identity ───────────────────────────────────────────────────────────────


def _anon_author_id(cookie: str) -> str:
    return ANON_AUTHOR_PREFIX + hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:32]


def _viewer_id(request: Request) -> str | None:
    cookie = request.cookies.get(ANON_COOKIE)
    return _anon_author_id(cookie) if cookie else None


def _writer_id(request: Request, response: Response) -> str:
    """The writing guest's author id, minting the cookie on their first write.

    Called only once every check has passed, so a refused write never tags the
    browser.
    """
    cookie = request.cookies.get(ANON_COOKIE)
    if not cookie:
        cookie = secrets.token_urlsafe(32)
        response.set_cookie(
            key=ANON_COOKIE, value=cookie, max_age=_ANON_COOKIE_MAX_AGE,
            httponly=True, samesite="lax", path="/api/public",
            # Same Secure rule as the session cookie: see the INVARIANT in auth.set_session_cookie.
            secure=_browser_scheme_is_https(request),
        )
    return _anon_author_id(cookie)


# ─── Gates ──────────────────────────────────────────────────────────────────


async def _require_comments(document_id: str) -> dict:
    """The write gate: the public funnel, then can_write_note as an anonymous principal."""
    ctx = await _require_doc(document_id)
    if not await can_write_note({"kind": "anonymous", "share": ctx}, document_id):
        raise HTTPException(status_code=403, detail="Comments are off for this link")
    return ctx


async def _note_session(document_id: str, session_id: str) -> dict:
    """A live note hosted by `document_id`, or a uniform 404."""
    session = await fetch_one("chat_sessions", session_id) if SAFE_ID_RE.match(session_id) else None
    if not session or session.get("is_note") is not True or session.get("document_id") != document_id:
        raise HTTPException(status_code=404, detail="Note not found")
    return session


async def _load_anchor_space(document_id: str) -> AnchorSpace:
    """The document's live text and anchor space, or 503 (the public read's rule)."""
    from ydoc_store import load
    try:
        return anchor_space(await load(document_id))
    except Exception as e:  # noqa: BLE001 — refuse, never anchor against a guess.
        logger.warning("anchor space failed for public doc %s: %r", document_id, e)
        raise HTTPException(status_code=503, detail="Document temporarily unavailable") from e


# ─── Wire shape ─────────────────────────────────────────────────────────────


def _public_message(wire: dict, viewer: str | None) -> dict:
    """A message as the public page sees it: no member or guest ids, ever."""
    return {
        "message_id": wire.get("message_id"),
        "parent_id": wire.get("parent_id"),
        "content": wire.get("content") or "",
        "created_at": _iso_or_none(wire.get("created_at")),
        "author_name": wire.get("author_name"),
        "is_mine": bool(viewer) and wire.get("author_id") == viewer,
    }


def _public_note(session: dict, anchor: dict | None, messages: list[dict], viewer: str | None) -> dict:
    return {
        "session_id": extract_id(session.get("id")) or session.get("session_id"),
        "created_at": _iso_or_none(session.get("created_at")),
        "anchor": anchor,
        "anchor_quote": session.get("anchor_quote"),
        "is_mine": bool(viewer) and session.get("user_id") == viewer,
        "messages": messages,
    }


def _resolved(space: AnchorSpace | None, session: dict) -> dict | None:
    """{state, start, end} in UTF-16 over the served text; None for an anchorless note."""
    start, end = session.get("anchor_rel_start"), session.get("anchor_rel_end")
    if space is None or not relpos_to_wire(start) or not relpos_to_wire(end):
        return None
    r = resolve_anchor(space, bytes(start), bytes(end))
    return {"state": r.state, "start": r.start, "end": r.end}


# ─── Handlers ───────────────────────────────────────────────────────────────


@router.get("/api/public/documents/{document_id}/notes")
async def list_public_notes(document_id: str, request: Request):
    """Every note on the document, with author names — only while comments are on."""
    ctx = await _require_doc(document_id)
    if not ctx["allow_comments"]:
        return {"allow_comments": False, "content_sha256": None, "notes": []}
    db = await get_db()
    sessions = await db.query(
        "SELECT * FROM chat_sessions WHERE is_note = true AND document_id = $did "
        "AND deleted_at IS NONE ORDER BY created_at",
        {"did": document_id},
    ) or []
    sids = [extract_id(s["id"]) for s in sessions]
    rows: list[dict] = []
    if sids:
        rows = await db.query(
            "SELECT * OMIT images FROM messages WHERE chat_id IN $sids AND deleted_at IS NONE "
            "ORDER BY created_at",
            {"sids": sids},
        ) or []
    by_sid = {extract_id(s["id"]): s for s in sessions}
    names = await user_names(db, {resolve_message_author(r, by_sid[r["chat_id"]]) for r in rows})
    space = (
        await _load_anchor_space(document_id)
        if any(s.get("anchor_rel_start") for s in sessions) else None
    )
    viewer = _viewer_id(request)
    grouped: dict[str, list[dict]] = {sid: [] for sid in sids}
    for r in rows:
        session = by_sid[r["chat_id"]]
        wire = serialize_record(r, "message_id")
        wire["author_id"] = resolve_message_author(r, session)
        wire["author_name"] = author_display(r, session, names)
        grouped[r["chat_id"]].append(_public_message(wire, viewer))
    notes = [_public_note(s, _resolved(space, s), grouped[sid], viewer) for sid, s in by_sid.items()]
    # The text the anchors were resolved against; the page paints them only while it
    # still shows that same text (null: no anchored note, nothing to compare).
    resolved_against = content_sha256(space.text) if space is not None else None
    return {"allow_comments": True, "content_sha256": resolved_against, "notes": notes}


async def _anchor_fields(document_id: str, body: PublicNoteCreate) -> tuple[dict, dict | None]:
    """The SessionCreate anchor fields built server-side from the guest's offsets."""
    if body.start is None:
        return {}, None
    space = await _load_anchor_space(document_id)
    # The guest anchored against the text the public payload served; any edit since
    # moves every offset, so a stale page is refused instead of anchoring elsewhere.
    if content_sha256(space.text) != body.content_sha256:
        raise HTTPException(status_code=409, detail="The document has changed — reload the page")
    try:
        start, end = build_anchor(space, body.start, body.end)
        quote = space.text[utf16_to_cp(space.text, body.start):utf16_to_cp(space.text, body.end)]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    fields = {
        "anchor_rel_start": relpos_to_wire(start),
        "anchor_rel_end": relpos_to_wire(end),
        "anchor_quote": quote,
    }
    return fields, {"state": "ok", "start": body.start, "end": body.end}


async def _flag_inbox(ctx: dict, session_id: str, document_id: str) -> None:
    """Mark the new guest note for the share's creator, else the project owner."""
    recipient = ctx.get("created_by")
    if not recipient:
        project = await fetch_one("projects", ctx["project_id"]) or {}
        recipient = project.get("owner_id")
    if recipient:
        await mark_arrived(
            KIND_NOTE, object_id=session_id, project_id=ctx["project_id"],
            document_id=document_id, recipient_id=recipient,
        )


@router.post("/api/public/documents/{document_id}/notes", status_code=201)
async def create_public_note(
    document_id: str, body: PublicNoteCreate, request: Request, response: Response,
):
    """A guest's new note: the session, its first message, and the owner's inbox flag."""
    ctx = await _require_comments(document_id)
    fields, anchor = await _anchor_fields(document_id, body)
    author_id = _writer_id(request, response)
    db = await get_db()
    created = await create_session_command(db, SessionCreate(
        project_id=ctx["project_id"], document_id=document_id, is_note=True, **fields,
    ), author_id)
    session_id = created["session_id"]
    session = await fetch_one("chat_sessions", session_id)
    try:
        message = await insert_user_message(
            db, session, session_id, content=body.content,
            author_id=author_id, author_name=body.author_name,
        )
    except Exception:
        # WHY: a guest's note exists only together with its first message. A failed
        # write would otherwise leave an empty note that no guest can delete (the
        # cookie is set only on a successful response), so the session goes with it.
        await delete_session_command(db, session_id, session)
        raise
    await _flag_inbox(ctx, session_id, document_id)
    return _public_note(session, anchor, [_public_message(message, author_id)], author_id)


@router.post("/api/public/documents/{document_id}/notes/{session_id}/messages", status_code=201)
async def reply_public_note(
    document_id: str, session_id: str, body: PublicReply, request: Request, response: Response,
):
    """A guest's reply in an existing note on this document."""
    await _require_comments(document_id)
    session = await _note_session(document_id, session_id)
    author_id = _writer_id(request, response)
    db = await get_db()
    message = await insert_user_message(
        db, session, session_id, content=body.content, author_id=author_id,
        author_name=body.author_name, parent_id=body.parent_id,
    )
    return _public_message(message, author_id)


@router.delete("/api/public/documents/{document_id}/notes/{session_id}/messages/{message_id}")
async def delete_public_message(document_id: str, session_id: str, message_id: str, request: Request):
    """A guest deletes their OWN leaf message (access.can_modify_note_message)."""
    await _require_comments(document_id)
    viewer = _viewer_id(request)
    if not viewer:
        raise HTTPException(status_code=403, detail="Cannot delete this note message")
    session = await _note_session(document_id, session_id)
    msg = await fetch_one("messages", message_id) if SAFE_ID_RE.match(message_id) else None
    if not msg or msg.get("chat_id") != session_id:
        raise HTTPException(status_code=404, detail="Message not found")
    ids = await delete_note_message(
        await get_db(), session, msg, message_id, actor_id=viewer, is_owner=False,
    )
    return {"success": True, "deleted_count": len(ids)}


@router.delete("/api/public/documents/{document_id}/notes/{session_id}")
async def delete_public_note(document_id: str, session_id: str, request: Request):
    """A guest deletes their OWN note — only while every live message in it is theirs."""
    await _require_comments(document_id)
    viewer = _viewer_id(request)
    session = await _note_session(document_id, session_id)
    db = await get_db()
    rows = await db.query(
        "SELECT author_id FROM messages WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": session_id},
    ) or []
    # INVARIANT(security): a guest deletes a whole note only when they created it and
    # nobody else has written in it. Why: deleting the session deletes every reply —
    # a guest must never remove a member's (or another guest's) words; a root does
    # that through the authed routes.
    if not viewer or session.get("user_id") != viewer or any(
        resolve_message_author(r, session) != viewer for r in rows
    ):
        raise HTTPException(status_code=403, detail="Cannot delete this note")
    await delete_session_command(db, session_id, session)
    return {"success": True}
