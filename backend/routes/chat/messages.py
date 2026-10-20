"""Message CRUD for chat sessions — list, create, update, delete (branch cascade)."""
# SYSTEM: chat-messages — message CRUD with note-chat ACL and branch-cascade delete

import logging
from collections import deque
from uuid import uuid4

from chat_sessions import recall as chat_recall
from chat_sessions.note_events import broadcast_doc_id, fire_note_emit
from fastapi import Depends, HTTPException, Query
from surrealdb import AsyncSurreal

import event_bus
from auth import get_current_user
from db import create_record, fetch_one, get_db, run_in_transaction
from models import MessageCreate, MessageUpdate
from routes.chat._router import router
from routes.chat.fanout import ensure_fanout
from routes.chat.serializers import (
    PREVIEW_CONTENT_MAX,
    _resolve_author_names,
    _serialize_message,
    _session_preview,
    _validate_image_url,
)
from routes.chat.sessions import _require_session_access

logger = logging.getLogger(__name__)


async def _emit_note_message_changed(
    db, session: dict, session_id: str, *, action: str,
    message: dict | None = None, message_ids: list[str] | None = None,
) -> None:
    try:
        if session.get("is_note") is not True:
            return
        entity_id = await broadcast_doc_id(db, session)
        preview = await _session_preview(db, session_id)
        frame: dict = {
            "type": "note_message_changed",
            "action": action,
            "session_id": session_id,
            "preview": preview,
        }
        if message is not None:
            frame["message"] = message
        if message_ids:
            frame["message_ids"] = message_ids
        await event_bus.emit("note_message_changed", entity_type="doc", entity_id=entity_id, event=frame)
    except Exception:
        logger.warning("note_message_changed emit failed", exc_info=True)


def schedule_note_message_changed(
    db, session: dict, session_id: str, *, action: str,
    message: dict | None = None, message_ids: list[str] | None = None,
) -> None:
    """Fire-and-forget wrapper: resolve + emit off the request path."""
    fire_note_emit(_emit_note_message_changed(
        db, session, session_id, action=action, message=message, message_ids=message_ids,
    ))


async def _is_project_owner(session: dict, user: dict) -> bool:
    """Whether the current user is a project root (owner, or admin with a member
    row) of this session's project.

    # ARCH: the project root has full
    # edit/delete power over any note message, including cascade delete. Derived
    # from access.is_project_root (projects.owner_id + the membership row) — NOT
    # from access_level: a full-access member who is not root is NOT root.
    """
    pid = session.get("project_id")
    if not pid:
        return False
    proj = await fetch_one("projects", pid)
    from access import is_project_root
    return await is_project_root(proj or {}, user)


# ─── Messages ─────────────────────────────────────────────────────────────────

# INVARIANT(data-loss): list_messages MUST NOT transfer the `images` column — it
# holds base64 data URIs (multi-MB per attachment), so a chat with attachments would
# ship megabytes on every open.  Why: images holds base64 data URIs (multi-MB each); a bare SELECT * would ship megabytes on every chat open, so OMIT images drops it from the list path. `OMIT images` drops it from `SELECT *` while
# `image_count` summarizes it; the client lazy-fetches the actual images from the
# per-message /images endpoint below. Why `SELECT * OMIT` over a hand-enumerated
# projection (cf. db.REF_META_COLUMNS): the messages table has 14+ columns
# (agent_trace, sources, …) that _serialize_message passes through verbatim;
# enumerating them risks silently dropping one on a future schema add. OMIT drops
# exactly the heavy column and auto-includes the rest.
# Keep this aligned with the /images endpoint (the only other reader of `images`).
#
# ARCH: OMIT also drops the
# three RETIRED timeline columns — `segments`, `agent_steps`, `reasoning`.
# They were a second copy of a turn's timeline; the driver's log replayed as
# `frames` is the timeline, and `halt` (the one fact the log cannot
# produce) rides its own column. The columns and their data are gone outright
# (messages_timeline_columns_drop — no row can carry them anymore);
# the OMIT stays as belt-and-braces, so even a row that somehow does can never
# ride the wire.
# `gen_steps` is NOT omitted and is not a fourth copy: it holds the DETACHED
# generate_image run's chips, built by a background task the driver's log never
# saw, so the replay cannot produce them; the reload mints them into the row's
# frames as `lore/image-gen` (see SYSTEM: dsh-conversation).
_MESSAGE_LIST_SELECT = (
    "SELECT *, array::len(images ?? []) AS image_count "
    "OMIT images, segments, agent_steps, reasoning FROM messages"
)


async def load_session_messages(
    db, session: dict, session_id: str, *, limit: int = 200, offset: int = 0,
    tail: bool = False,
) -> list[dict]:
    """Fetch + serialize a session's messages: the images-omitting projection
    and author-name resolution.

    Shared by list_messages and the sessions-list piggyback (which collapses the
    sessions→messages waterfall) so both paths agree on projection + shape. Pure
    read: the projection is written only by the live reducer (see list_messages).

    tail=True (notes) anchors the window at the NEWEST row: the query flips to
    DESC and the rows are reversed back to ASC before serialization, so
    offset=0 serves the newest `limit` rows and a growing offset pages
    BACKWARD toward the head. Why: a front-anchored window on a thread longer
    than `limit` silently hides the newest rows — and everything that parents
    onto "the last loaded row" (note replies) would parent onto the wrong tail.
    Notes have no agent timeline, so the offset>0 refusal in _attach_timeline
    never applies to them (the is_note early-return precedes it); AI chats keep
    the front anchor.
    """
    order = "DESC" if tail else "ASC"
    rows = await db.query(
        _MESSAGE_LIST_SELECT + " WHERE chat_id = $cid AND deleted_at IS NONE "
        f"ORDER BY created_at {order} LIMIT $limit START $offset",
        {"cid": session_id, "limit": limit, "offset": offset},
        site="chat",
    ) or []
    if tail:
        rows.reverse()
    author_names = await _resolve_author_names(db, session, rows)
    out = [_serialize_message(r, session, author_names) for r in rows]
    return out


@router.get(
    "/sessions/{session_id}/messages",
    responses={400: {"description": "A paginated read (offset>0) cannot anchor "
                                    "the agent timeline and is refused (AI "
                                    "chats only — note threads paginate "
                                    "tail-anchored and accept offset)"},
               502: {"description": "The agent transcript or its status is "
                                    "unreadable — the thread is never served "
                                    "without its timeline"}},
)
async def list_messages(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Return a chat session's messages (paginated) and its queue —
    `{messages, queue, queue_seq}`: the live dsh agent's next-turn inbox and
    the seq that snapshot includes (empty/None for a note chat).

    Pagination anchors differ by kind: AI chats are front-anchored (oldest
    first, offset>0 refused — the timeline cannot resolve mid-thread); note
    threads are TAIL-anchored (offset=0 serves the newest page, offset pages
    toward the head) so a long note never hides its newest rows."""
    session = await _require_session_access(session_id, user)
    out = await load_session_messages(
        db, session, session_id, limit=limit, offset=offset,
        tail=session.get("is_note") is True,
    )
    queue = await _attach_timeline(session, session_id, out, offset=offset)
    return {"messages": out, **queue}


# ARCH: the TRANSCRIPT is the driver's session log, not a Lore column. The Lore row owns
# what is ours — the id, the author, created_at, the sources panel, the
# images, and the abnormal-end halt card — and the ordered
# timeline (text, reasoning, tool chips, the neutral dsh chips) rides here as
# `frames`, replayed by the plugin through the same mapEvent the live stream
# used. The frontend reduces the live stream and these frames with ONE
# reducer, so a reloaded turn and the streamed turn agree by construction.
#
# INVARIANT(corruption): a row receives the frames of the turn its OWN
# `driver_seq` names, never another turn's. Why: a session's rows resolve
# only against the session's OWN log (dsh id = Lore id; a seeded branch's
# log carries the copied prefix under the SAME seqs), so the per-session stamp map below is the whole
# pairing (an unseeded branch reads its seed source's log instead — see
# _fetch_session_turns). Rows whose stamp does not resolve (a pre-harness
# turn, an abnormally ended turn) keep no `frames` key and the
# client renders what the row carries.
def _assign_frames_by_stamp(assistants: list[dict], turns: list[dict]) -> None:
    """Key each assistant row to its turn by the driver's own id — the row's
    `driver_seq` over the projection's per-turn `end_seq`, within THIS
    session's own log (the per-session stamp map)."""
    by_seq: dict[int, dict] = {}
    for t in turns:
        s = t.get("end_seq")
        if isinstance(s, int) and not isinstance(s, bool):
            by_seq[s] = t
    for r in assistants:
        t = by_seq.get(r.get("driver_seq"))
        if t is not None:
            # The replay of a NON-live session ends every closed turn with a
            # seq-anchored `turn_closed` (plugin entries.ts `terminals`) —
            # TRANSPORT state for the resync consumer (the channel's dispatch
            # arm), never timeline content: the rows' frames are the
            # assembler's input, so it is dropped here.
            r["frames"] = [
                f for f in t.get("frames") or []
                if not (isinstance(f, dict) and f.get("type") == "turn_closed")
            ]


def _log_shows_open_turn(turns: list[dict]) -> bool:
    """Whether the projection's trailing turn is still open (no `end_seq`)."""
    return bool(turns) and isinstance(turns[-1], dict) and turns[-1].get("end_seq") is None


def _mark_open_turn(
    assistants: list[dict], turns: list[dict], *, running: bool,
) -> None:
    """Attach the OPEN turn (the projection's trailing turn with no `end_seq`)
    to the row that turn is writing, flagged
    `open_turn`: the reload's streaming re-seat input (a reload mid-turn
    renders the open turn as STREAMING, never a settled partial row that
    flips on the next live frame). The open turn's `assistant_stream` (the
    plugin's live-stream fold — the streamed text the log does not hold yet)
    passes through VERBATIM beside the mark: the browser seats the transient
    tail from it. Only the trailing OPEN turn's field is read — a closed
    turn never carries one (entries.ts attaches it to the open turn only).

    # INVARIANT(replay-split, corruption, read side): a row that carries `halt`
    # never takes the open mark. Why: the plugin's read closes a dead turn's
    # log with dsh's closers, so a trailing open turn marks a REGISTERED turn —
    # and the one dead shape left is the breach whose /stop failed while the
    # plugin turn still runs (halt card written, session live). Beside that
    # card the open frames would render TWO records for one turn; the halt
    # column is the splitter, mirroring entries.ts's emitting branch.
    """
    if not _log_shows_open_turn(turns):
        if running:
            _mark_running_turn(assistants)
        return
    open_frames = [
        f for f in turns[-1].get("frames") or [] if isinstance(f, dict)
    ]
    candidates = [
        r for r in assistants
        if r.get("halt") is None
        and r.get("driver_seq") is None
        and r.get("frames") is None
    ]
    if not candidates:
        # The trailing turn's row does not exist here (a stamped row already
        # claimed the head, or the open turn is unclaimed — a backend restart
        # mid-turn): nothing claims the open turn this read.
        return
    newest = max(candidates, key=lambda r: r.get("created_at") or "")
    newest["frames"] = list(open_frames)
    newest["open_turn"] = True
    baseline = turns[-1].get("assistant_stream")
    if isinstance(baseline, dict):
        newest["assistant_stream"] = baseline


def _mark_running_turn(assistants: list[dict]) -> None:
    """Mark the running turn the log cannot show yet: the NEWEST assistant row,
    only if it is the open turn's shape (unstamped, no halt, no frames).
    No frames and no `assistant_stream` — nothing streamed is
    logged yet."""
    newest = max(assistants, key=lambda r: r.get("created_at") or "")
    # INVARIANT(corruption): a running dsh agent marks the open turn even when
    # the driver log shows none. Why: between dsh's claim and its log flush
    # the timeline cannot show the turn, and an unmarked reload makes the
    # browser close a live turn as 'lost'. The newest
    # row only — an older abnormally-ended unstamped row is never marked; a
    # `halt` row never is (the halt INVARIANT above keeps priority).
    if (
        newest.get("halt") is None
        and newest.get("driver_seq") is None
        and newest.get("frames") is None
    ):
        newest["frames"] = []
        newest["open_turn"] = True


async def _fetch_session_turns(session: dict, session_id: str) -> dict:
    """The driver's replayed session for this thread's log — the plugin's
    ReplayedSession `{turns, tail_seq}`.

    The turns of a compaction continuation run under the SOURCE session's log
    (`completions.py`: `compacted_from or session_id`), so the read names the
    same lineage — asking for the continuation's own chat id yields an empty
    turn list for an id the driver has never seen, and the rows never get
    frames. The returned id is also the mint identity the reload lore mints
    key on (the compaction window's source side).
    """
    from driver.timeline import DriverTimelineUnavailable, fetch_session_entries
    lineage = session.get("compacted_from") or session_id
    seed_seq = session.get("seed_source_seq")
    seeded_from = (
        session.get("seed_source_session")
        if isinstance(seed_seq, int) and not isinstance(seed_seq, bool) else None
    )
    if seeded_from:
        # INVARIANT(corruption): an unseeded branch reads its SEED SOURCE log.
        # Why: the copies keep the source's seqs, so the source's turns up to
        # the boundary ARE the prefix's frames; the branch's own (empty) log
        # renders the prefix text-only — every chip gone until a first turn
        # succeeds. The read is cut at the boundary (_cut_at_seed) so the
        # source's later turns or live open turn never graft onto the branch.
        lineage = seeded_from
    try:
        replay = await fetch_session_entries(lineage)
    except DriverTimelineUnavailable as exc:
        # No silent degradation: an unreadable transcript is an explicit
        # failure, never an empty thread (DriverTimelineUnavailable's
        # INVARIANT names why).
        logger.warning("agent timeline unavailable session=%s: %s", session_id, exc)
        raise HTTPException(
            status_code=502, detail="The agent timeline could not be read",
        ) from exc
    if seeded_from:
        replay = _cut_at_seed(replay or {}, seed_seq)
    return {"lineage": lineage, **(replay or {})}


async def _read_session_status(session: dict, session_id: str) -> dict:
    """The live dsh agent's `{status, queue, seq}` for this chat's DRIVER
    session (compacted_from or the chat id — the session its sends queue in).
    An unreadable status is a 502, the same arm as an unreadable timeline:
    the reload's open-turn mark and queue derive from it."""
    from driver.timeline import DriverTimelineUnavailable, fetch_session_status
    driver_session_id = session.get("compacted_from") or session_id
    try:
        status = await fetch_session_status(driver_session_id)
    except DriverTimelineUnavailable as exc:
        logger.warning("agent status unavailable session=%s: %s", session_id, exc)
        raise HTTPException(
            status_code=502, detail="The agent timeline could not be read",
        ) from exc
    if status["status"] == "running" or status["queue"]:
        await _ensure_fanout_quietly(session_id, driver_session_id)
    return status


async def _ensure_fanout_quietly(session_id: str, driver_session_id: str) -> None:
    """Re-attach the fan-out for a session whose turns run or wait. WHY: the
    subscription lives in memory and only a completion opened it, so after a
    backend restart the queue's next claims would reach no listener and get
    no rows. Best-effort: the read itself must not fail over it — the next
    send opens it anyway."""
    try:
        await ensure_fanout(session_id, driver_session_id=driver_session_id)
    except Exception:
        logger.warning(
            "fan-out re-attach failed session=%s", session_id, exc_info=True)


def _cut_at_seed(replay: dict, seed_seq: int) -> dict:
    """The seed source's replay as the unseeded branch sees it: closed turns
    ending at or before the boundary only (an open turn is the source's own
    live turn, never this branch's), the tail capped at the boundary."""
    turns = [
        t for t in replay.get("turns") or []
        if isinstance(t, dict) and isinstance(t.get("end_seq"), int)
        and not isinstance(t.get("end_seq"), bool) and t["end_seq"] <= seed_seq
    ]
    tail = replay.get("tail_seq")
    if isinstance(tail, int) and tail > seed_seq:
        tail = seed_seq
    return {**replay, "turns": turns, "tail_seq": tail}


async def _attach_timeline(
    session: dict, session_id: str, out: list[dict], *, offset: int,
) -> dict:
    """Stamp each assistant row with the driver's frames for its turn, then
    mint the reload-side lore events onto the rows. Returns the chat's queue
    — `{queue, queue_seq}` from the live dsh agent, read also for a chat with
    no rows yet (a first message may be queued).

    A note thread never runs a turn — no driver read. A thread with no
    assistant row reads only the status (its first message may be queued).
    Rows the log has no turn for (threads predating
    the harness, an abnormally ended turn that never wrote `turn/end`) keep the Lore row exactly as it is: no `frames`
    key from the replay — and the reload mints add exactly what the row owns
    (the `halt` column's card at the log tail; a gen_steps image run whose
    dispatching call the replay carries). The client feeds the rows' frames
    to the assembler, so the mints ride the same input as the replay.
    """
    if session.get("is_note") is True:
        return {"queue": [], "queue_seq": None}
    assistants = [m for m in out if m.get("role") == "assistant"]
    if assistants and offset:
        # The page starts mid-thread, so the page's rows alone cannot anchor
        # the pairing. Refusing is the honest answer — a silently
        # mis-aligned timeline would show one turn's tool calls under
        # another's answer.
        raise HTTPException(
            status_code=400,
            detail="Paginated message reads cannot resolve the agent timeline; use offset=0",
        )
    if not assistants:
        status = await _read_session_status(session, session_id)
        return {"queue": status["queue"], "queue_seq": status["seq"]}
    replay = await _fetch_session_turns(session, session_id)
    turns = replay.get("turns") or []
    tail_seq = replay.get("tail_seq")
    logged = bool(turns) or tail_seq is not None
    if logged:
        _assign_frames_by_stamp(assistants, turns)
    status = await _read_session_status(session, session_id)
    # The open turn: AFTER the stamp pass — its row is by definition
    # unstamped, so the two never contend for one row.
    _mark_open_turn(assistants, turns, running=status["status"] == "running")
    if logged:
        from driver.frames import attach_reload_lore_mints
        await attach_reload_lore_mints(
            assistants, tail_seq=tail_seq,
            session_id=replay.get("lineage") or session_id,
        )
    return {"queue": status["queue"], "queue_seq": status["seq"]}

@router.get("/sessions/{session_id}/messages/{message_id}/images")
async def get_message_images(
    session_id: str,
    message_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """Return the base64 image data URIs for a single message (lazy-fetched by the
    client because list_messages omits them — see _MESSAGE_LIST_SELECT).

    Guarded by the same session ACL as the list, and the message must belong to the
    session (404 otherwise) so the path cannot leak another session's attachments.
    Returns images in the SAME data-URI form MessageCreate.images accepts.
    """
    await _require_session_access(session_id, user)
    rows = await db.query(
        "SELECT images FROM messages "
        "WHERE meta::id(id) = $mid AND chat_id = $cid AND deleted_at IS NONE",
        {"mid": message_id, "cid": session_id},
        site="chat",
    ) or []
    if not rows:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"images": rows[0].get("images") or []}


@router.get("/sessions/{session_id}/last-message-preview")
async def last_message_preview(session_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Lazy hover-preview source for the chat-list plaque: the newest non-deleted
    message content, truncated in SQL.

    WHY a dedicated endpoint (not list_sessions): materializing message content
    for EVERY listed AI chat scales with total message volume (long threads) — the
    session list deliberately carries only a content-free last-activity timestamp
    for AI chats (see _last_activity_map in sessions.py). This fetches ONE row for
    ONE hovered session on demand, so the list stays cheap under pagination.

    INVARIANT: string::slice(content, 0, PREVIEW_CONTENT_MAX) truncates server-side.
    Why: message bodies may embed large image data URLs — never transfer the full
    body for a preview (PREVIEW_CONTENT_MAX = 2000; mirrors _note_previews' SQL
    truncation, which also uses the constant).
    """
    await _require_session_access(session_id, user)
    rows = await db.query(
        # WHY select created_at: SurrealDB requires ORDER BY fields be in the
        # projection (same constraint as _latest_chat_field in sessions.py).
        f"SELECT string::slice(content, 0, {PREVIEW_CONTENT_MAX}) AS content, created_at FROM messages "
        "WHERE chat_id = $id AND deleted_at IS NONE "
        "ORDER BY created_at DESC LIMIT 1",
        {"id": session_id},
    )
    preview = ((rows[0].get("content") if rows else None) or "").strip() or None
    return {"preview": preview}


async def _validate_message_refs(
    session_id: str, parent_id: str | None, images: list[str] | None,
) -> None:
    """422 on a parent outside the session, 400 on an image URL that is not allowed."""
    if parent_id:
        # INVARIANT(security): parent_id must belong to THIS session. Why: an
        # unvalidated parent let a caller splice a message under a different chat's tree.
        parent = await fetch_one("messages", parent_id)
        if not parent or parent.get("chat_id") != session_id:
            raise HTTPException(status_code=422, detail="parent_id must belong to this session")
    if images:
        # SSRF guard — the completion path validates images; this endpoint did not,
        # so arbitrary http:// / file:// / internal-IP URLs got persisted. Reuse the
        # shared helper (contract: raises 400 on the first invalid URL).
        for url in images:
            _validate_image_url(url)


async def insert_user_message(
    db, session: dict, session_id: str, *, content: str, author_id: str | None = None,
    author_name: str | None = None, parent_id: str | None = None,
    images: list[str] | None = None,
) -> dict:
    """Write one user message under an already-gated session and return it serialized.

    The one writer of user message rows for the authed and the public note surface:
    it validates the parent, touches the session and fires the note realtime nudge,
    so a member's panel hears a guest's reply exactly like a member's.
    """
    await _validate_message_refs(session_id, parent_id, images)
    data: dict = {
        "chat_id": session_id,
        "parent_id": parent_id,
        "role": "user",
        "content": content,
    }
    if author_id:
        data["author_id"] = author_id
    if author_name:
        data["author_name"] = author_name
    if images:
        data["images"] = images
    row = await create_record("messages", str(uuid4()), data)
    # Touch session updated_at
    await db.query(
        "UPDATE type::record('chat_sessions', $id) SET updated_at = time::now()",
        {"id": session_id},
    )
    # ARCH: note `title` is never
    # copied from the first message. Why: the column was a one-shot 100-char
    # snapshot that the backend never refreshed, so it showed stale "copied lines"
    # after edits/deletes. Note display is now derived live from messages
    # (see list_sessions preview aggregate). `title` is left untouched for notes;
    # AI chats get theirs from the harness titler's relayed session/title event.
    author_names = await _resolve_author_names(db, session, [row])
    out = _serialize_message(row, session, author_names)

    schedule_note_message_changed(db, session, session_id, action="added", message=out)
    return out


@router.post("/sessions/{session_id}/messages", status_code=201)
async def create_message(session_id: str, body: MessageCreate, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Create a single message (user or assistant).

    For note-chats: the message author_id is stamped to the current user. The
    role is forced to 'user' (notes do not have assistant turns). Body author_id
    is ignored -- callers cannot impersonate other users.
    """
    session = await _require_session_access(session_id, user)
    author_id: str | None = body.author_id
    # INVARIANT(security): this endpoint only ever creates USER messages. Why: for AI
    # chats, body.role accepted verbatim let a client inject fabricated assistant/system
    # content into its own transcript, which then feeds the agent transcript on later
    # turns. Assistant rows are created ONLY by the completion stream. Note-chats
    # already forced 'user' (notes have no assistant turns); this extends it to AI chats.
    if session.get("is_note") is True:
        author_id = user["user_id"]
    return await insert_user_message(
        db, session, session_id, content=body.content, author_id=author_id,
        parent_id=body.parent_id, images=body.images,
    )


@router.patch("/messages/{message_id}")
async def update_message(message_id: str, body: MessageUpdate, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Update message content (edit-in-place).

    For note-chats: the message author OR a project root (owner, or admin with a
    member row) may edit (see `backend/access.py:can_modify_note_message`). The
    root is resolved via `_is_project_owner` (is_project_root, NOT access_level).
    """
    msg = await fetch_one("messages", message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    session = await _require_session_access(msg["chat_id"], user)
    if session.get("is_note") is True:
        from access import can_modify_note_message
        is_owner = await _is_project_owner(session, user)
        if not can_modify_note_message(
            action="edit", user_id=user["user_id"],
            is_owner=is_owner, message=msg, session=session,
        ):
            raise HTTPException(status_code=403, detail="Only the author or project owner may edit a note")
    sets: list[str] = ["content = $content"]
    params: dict = {"id": message_id, "content": body.content}
    if body.sources is not None:
        sets.append("sources = $sources")
        params["sources"] = body.sources
    rows = await db.query(
        f"UPDATE type::record('messages', $id) SET {', '.join(sets)} RETURN AFTER",
        params,
    )
    if isinstance(rows, str):
        raise HTTPException(status_code=500, detail=f"Database error: {rows}")
    if not rows or not rows[0]:
        raise HTTPException(status_code=500, detail="Failed to update message")
    author_names = await _resolve_author_names(db, session, [rows[0]])
    out = _serialize_message(rows[0], session, author_names)

    schedule_note_message_changed(
        db, session, msg["chat_id"], action="edited", message=out,
    )
    if session.get("is_note") is not True:
        await chat_recall.schedule_chat_index(session["user_id"], session["project_id"])
    return out


def _collect_subtree(children: dict[str, list[str]], root: str) -> list[str]:
    """BFS down parent_id from `root`, returning [root, ...descendants] in BFS order.

    The note-chat cascade's walk. SurrealQL lacks recursive parent_id
    traversal, so the walk is in Python over the pre-loaded parent→children
    map."""
    ids: list[str] = []
    queue: deque[str] = deque([root])
    while queue:
        mid = queue.popleft()
        ids.append(mid)
        queue.extend(children.get(mid, []))
    return ids


async def _soft_delete_leaf_guarded(db, message_id: str) -> None:
    """Soft-delete a single LEAF row inside a transactional IF/THROW guard.

    # INVARIANT: the non-owner single-row delete is guarded inside the same
    # transactional RPC that performs the soft-delete, so a reply arriving between
    # the map load and the UPDATE cannot orphan it (→ 409). Why: the
    # leaf-vs-cascade split creates this TOCTOU
    # surface; the in-DB IF/THROW closes it without regressing the hot
    # owner-cascade / AI-cascade paths. Raises HTTPException(409) on has_children.
    """
    try:
        await run_in_transaction(db, [
            "LET $kids = (SELECT VALUE id FROM messages "
            "WHERE parent_id = $id AND deleted_at IS NONE)",
            "IF array::len($kids) = 0 { "
            "  UPDATE messages SET deleted_at = time::now() "
            "  WHERE meta::id(id) = $id AND deleted_at IS NONE "
            "} ELSE { THROW 'has_children' }",
        ], {"id": message_id})
    except RuntimeError as e:
        if "has_children" in str(e):
            raise HTTPException(
                status_code=409,
                detail="Cannot delete a message that has replies",
            )
        raise


async def _delete_branch(
    *, db, session: dict, msg: dict, message_id: str,
    children: dict[str, list[str]], actor_id: str, is_owner: bool,
) -> list[str]:
    """Resolve the deletion scope + soft-delete the branch; return the deleted ids.

    Note-chat own-only ACL: owner → subtree cascade, non-owner author → guarded
    single leaf (ACL resolved before soft-deleting). Split out of
    delete_message_branch so the route stays under the 50-line function cap.
    """
    from access import can_modify_note_message
    from models import resolve_message_author
    has_children = bool(children.get(message_id))
    if not can_modify_note_message(
        action="delete", user_id=actor_id,
        is_owner=is_owner, has_children=has_children,
        message=msg, session=session,
    ):
        # Distinguish "author of a non-leaf" (409, precise) from a plain
        # permission denial (403). The owner never lands here.
        is_author = resolve_message_author(msg, session) == actor_id
        if is_author and has_children:
            raise HTTPException(
                status_code=409,
                detail="Cannot delete a message that has replies",
            )
        raise HTTPException(status_code=403, detail="Cannot delete this note message")
    # Owner → cascade over the subtree; non-owner author → single leaf row.
    if is_owner:
        ids_to_delete: list[str] = _collect_subtree(children, message_id)
        await db.query(
            "UPDATE messages SET deleted_at = time::now() WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
            {"ids": ids_to_delete},
        )
    else:
        await _soft_delete_leaf_guarded(db, message_id)
        ids_to_delete = [message_id]
    return ids_to_delete


async def delete_note_message(
    db, session: dict, msg: dict, message_id: str, *, actor_id: str, is_owner: bool,
) -> list[str]:
    """Delete a note message as `actor_id` (root → cascade, author → own leaf) and
    nudge note listeners; returns the deleted ids. The caller has gated the session.

    # WHY: Two-phase cascade delete. Phase 1: load parent-child map. Phase 2:
    # BFS in memory + batch soft-delete (in _delete_branch). The SELECT doesn't
    # mutate, so a crash between phases means no partial state — the user can
    # retry. The UPDATE is a single SurrealDB statement (atomic). BFS is done in
    # Python because SurrealQL lacks recursive traversal on the parent_id field.
    """
    all_rows = await db.query(
        "SELECT meta::id(id) AS mid, parent_id FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": msg["chat_id"]},
    )
    children: dict[str, list[str]] = {}
    for r in (all_rows or []):
        pid = r.get("parent_id")
        if pid:
            children.setdefault(pid, []).append(r["mid"])

    ids_to_delete = await _delete_branch(
        db=db, session=session, msg=msg, message_id=message_id,
        children=children, actor_id=actor_id, is_owner=is_owner,
    )
    schedule_note_message_changed(
        db, session, msg["chat_id"], action="deleted", message_ids=ids_to_delete,
    )
    return ids_to_delete


@router.delete("/messages/{message_id}")
async def delete_message_branch(message_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """Soft-delete a message and all its descendants (cascade via BFS on parent_id).

    WHY: Two-query cascade delete -- loads all live message IDs for the session,
    builds parent->children map in memory, BFS from target, then batch soft-delete.
    Always exactly 2 DB queries regardless of tree depth.

    For note-chats: owner (root) may delete any message (cascade); a non-owner
    author may delete only their own LEAF message (single row, no cascade). See
    backend/access.py:can_modify_note_message. AI chat (is_note=False) is
    REFUSED (400): a branch session only appends — there is no branch surgery.
    """
    msg = await fetch_one("messages", message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    session = await _require_session_access(msg["chat_id"], user)
    # ARCH: an AI chat's rows only ever APPEND —
    # there is no message delete. A branch is abandoned by leaving it (the
    # switcher still lists it), and the whole thread dies with its root via
    # DELETE /sessions/{thread_root}. Note chats keep their delete untouched.
    if session.get("is_note") is not True:
        raise HTTPException(
            status_code=400,
            detail="AI chats have no message delete — delete the conversation, "
                   "not a message",
        )

    ids_to_delete = await delete_note_message(
        db, session, msg, message_id,
        actor_id=user["user_id"], is_owner=await _is_project_owner(session, user),
    )
    return {"success": True, "deleted_count": len(ids_to_delete)}
