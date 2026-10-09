"""List-sessions read path for chat sessions (the GET /sessions aggregate).

Part of the chat-sessions system: the candidate load,
ref/title/preview/last-activity aggregates, the last-message sort, and the
active-messages piggyback — the read half of session listing. CRUD routes and
access gates stay in `sessions.py`; the session wire shape is
`chat_sessions.serialize`. No SYSTEM marker (internal extraction; the chat-sessions entry stays
in sessions.py).
"""
import logging

from chat_sessions.serialize import build_ref_map, serialize_session
from fastapi import Depends, HTTPException, Query
from surrealdb import AsyncSurreal

from access import get_document_access
from auth import get_current_user
from db import extract_id, fetch_one, get_ancestor_ids, get_db
from models import is_ref_row
from routes.chat._router import router
from routes.chat.serializers import PREVIEW_CONTENT_MAX

logger = logging.getLogger(__name__)


# ARCH: candidate cap for AI chat listing.
# AI chats are listed PROJECT-WIDE per (project, user) — the open document is
# not a server-side visibility filter. This constant bounds the candidate
# set (the newest MAX_LISTED_SESSIONS chats by updated_at) BEFORE the per-chat
# last-activity aggregate runs, so ai_agg scans at most this many chats instead of
# every chat the user ever had in the project. Same value as the endpoint default
# `limit`: the list is "the first page", never "everything".
MAX_LISTED_SESSIONS = 200


async def _note_scope_ids(db, document_id: str, project_id: str) -> list[str]:
    """Notes: KEEP the branch scope walk (document + ancestor reference-children,
    or reference + parent + siblings). Shared comment threads stay scoped."""
    scope_ids: list[str] = [document_id]
    owner = await fetch_one("documents", document_id)
    if owner and is_ref_row(owner):
        parent_id = owner.get("parent_id")
        if parent_id:
            scope_ids.append(parent_id)
            siblings = await db.query(
                "SELECT VALUE meta::id(id) FROM documents "
                "WHERE parent_id = $did AND is_reference = true AND deleted_at IS NONE",
                {"did": parent_id},
            )
            scope_ids.extend(str(c) for c in (siblings or []))
    elif owner and not is_ref_row(owner):
        ancestor_ids = await get_ancestor_ids(document_id, project_id)
        children = await db.query(
            "SELECT VALUE meta::id(id) FROM documents "
            "WHERE parent_id IN $ids AND is_reference = true AND deleted_at IS NONE",
            {"ids": ancestor_ids},
        )
        scope_ids.extend(str(c) for c in (children or []))
    return scope_ids


async def _load_candidate_rows(
    db, *, project_id: str, user: dict, document_id: str, is_note: bool | None,
) -> list:
    """The candidate THREAD rows for the list. Note-scoped (branch walk) or AI
    project-wide per (project, user), candidate-capped before the last-activity
    aggregate. No document_id scope filter for AI chats.

    # ARCH: the AI list counts THREADS — the
    # candidates are ROOT rows only (thread_id = own id, or IS NONE for a
    # pre-migration row that is its own thread), so branches never eat the
    # MAX_LISTED_SESSIONS cap; the displayed fields come from each thread's
    # last-opened branch (_thread_display_rows).
    """
    if is_note is True:
        scope = await _note_scope_ids(db, document_id, project_id)
        return await db.query(
            "SELECT * FROM chat_sessions "
            "WHERE project_id = $pid AND document_id IN $scope "
            "AND is_note = true AND deleted_at IS NONE "
            "ORDER BY updated_at DESC",
            {"pid": project_id, "scope": scope},
        )
    return await db.query(
        "SELECT * FROM chat_sessions "
        "WHERE project_id = $pid AND user_id = $uid "
        "AND is_note = false AND deleted_at IS NONE "
        "AND (thread_id IS NONE OR thread_id = meta::id(id)) "
        "ORDER BY updated_at DESC LIMIT $max",
        {"pid": project_id, "uid": user["user_id"], "max": MAX_LISTED_SESSIONS},
    )


async def _thread_display_rows(db, roots: list) -> list[tuple[str, dict]]:
    """Each listed AI THREAD renders its last-opened branch: the root's
    active_branch_id row when it is live and of the same thread (else the root
    itself). Returns (root_id, display_row) pairs — the root id keys thread
    identity (a piggyback hint may name either side), the display row feeds
    the serializer and the last-activity aggregate.

    # INVARIANT(corruption): a branch whose thread_id does not match the root
    # is IGNORED (the root previews itself). Why: active_branch_id is a bare
    # pointer written by PATCH; a stale or corrupt pointer must never make the
    # list preview ANOTHER user's thread.
    """
    wanted = {
        str(r.get("active_branch_id"))
        for r in (roots or []) if r.get("active_branch_id")
    }
    branch_map: dict[str, dict] = {}
    if wanted:
        rows = await db.query(
            "SELECT * FROM chat_sessions "
            "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
            {"ids": list(wanted)},
        )
        for r in (rows or []):
            branch_map[str(extract_id(r.get("id")))] = r
    out: list[tuple[str, dict]] = []
    for root in (roots or []):
        rid = extract_id(root.get("id"))
        ab = str(root.get("active_branch_id") or "")
        branch = branch_map.get(ab)
        if (
            branch
            and branch.get("is_note") is not True
            and (branch.get("thread_id") or ab) == (root.get("thread_id") or rid)
        ):
            out.append((rid, branch))
        else:
            out.append((rid, root))
    return out


async def _document_titles(db, rows: list, ref_map: dict) -> dict[str, str]:
    """Document-session titles in ONE batched query (thin client).
    Only genuine document-sessions carry a document_title; ref-sessions are labelled
    from reference_title (their OWN title, in ref_map) and are skipped here. The
    serializer consumes this map with zero DB calls of its own (its purity INVARIANT
    is preserved)."""
    owning_doc_ids: set[str] = set()
    for r in (rows or []):
        did = str(r.get("document_id") or "")
        if not did or is_ref_row(ref_map.get(did) or {}):
            continue
        owning_doc_ids.add(did)
    document_titles: dict[str, str] = {}
    if owning_doc_ids:
        title_rows = await db.query(
            "SELECT meta::id(id) AS id, title FROM documents "
            "WHERE meta::id(id) IN $ids AND deleted_at IS NONE",
            {"ids": list(owning_doc_ids)},
        )
        for rr in (title_rows or []):
            tid = str(rr.get("id") or "")
            if tid:
                document_titles[tid] = rr.get("title")
    return document_titles


async def _note_previews(db, note_ids: list[str]) -> dict[str, dict]:
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


async def _last_activity_map(db, ai_ids: list[str], previews: dict) -> dict:
    """Unified last-activity map for ALL sessions (notes + AI). Seeded from the note
    preview aggregate (its `last_at` is already the newest message created_at per
    note), then filled for AI chats by a content-free timestamp scan. One map feeds
    the serializer (last_message_at on every card) AND the Python sort.

    # WHY: messages.chat_id is the BARE session UUID (see the note block
    # Why: bare keys are what `chat_id IN $ids` and out["session_id"] both use.
    # above) — keys here are bare ids from extract_id(), matching the
    # out["session_id"] the serializer looks up.
    #
    # ARCH: the AI aggregate scans ONLY
    # user messages (role='user'), so last_message_at reflects the last USER
    # message time — immutable across the assistant reply and the update_session
    # updated_at bump. Why: an assistant reply (or a settings
    # PATCH) otherwise reorders the list on every enter→leave, and the value
    # would flicker vs the optimistic send value (which is the user-msg time).
    # The notes preview path above is UNCHANGED — notes keep last-message-of-any-
    # role (out of scope).
    """
    last_at_by_session: dict[str, object] = {
        sid: pv.get("last_at")
        for sid, pv in previews.items()
        if pv.get("last_at")
    }
    if not ai_ids:
        return last_at_by_session
    # Aggregate per-chat max(created_at) in SQL so only ~N rows transfer (one
    # per chat) instead of every message of every listed AI thread. time::unix()
    # converts the datetime to a number BEFORE math::max — math::max on raw
    # datetimes coerces them to floats and returns -inf (documented in the
    # note block above) — then time::from_unix() restores a datetime. AI
    # threads can be long, so materializing every row (as the note preview
    # must, since it needs content) would scale with total message volume.
    ai_agg = await db.query(
        "SELECT chat_id, time::from_unix(math::max(time::unix(created_at))) AS last_at "
        "FROM messages WHERE chat_id IN $ids AND role = 'user' AND deleted_at IS NONE "
        "GROUP BY chat_id",
        {"ids": ai_ids},
    )
    for m in (ai_agg or []):
        cid = str(m.get("chat_id", ""))
        last = m.get("last_at")
        if cid and last:
            last_at_by_session[cid] = last
    return last_at_by_session


async def _piggyback_messages(db, target_row: dict) -> dict | None:
    """The active session's rows for the sessions index — the SAME payload
    `GET /sessions/{id}/messages` serves, timeline included.

    Returns None when the transcript is unreadable: the client's null-branch
    falls back to `loadMessages`, which surfaces the named 502. Degrading the
    piggyback keeps the session INDEX alive when one transcript is down, and it
    is not silent — the fallback still shows the user the real error.
    """
    from routes.chat.messages import (  # local: avoid import cycle
        _attach_timeline,
        load_session_messages,
    )
    tid = extract_id(target_row.get("id"))
    msgs = await load_session_messages(db, target_row, tid)
    # INVARIANT (timeline parity): every path that returns a session's rows
    # returns the SAME timeline. Why: the piggyback is the restore path of a
    # page reload / second tab, and committing rows without `frames` there
    # rendered the session text-only until a manual re-select (observed: the
    # same row frames=104 via GET /messages, ABSENT via the piggyback).
    try:
        await _attach_timeline(target_row, tid, msgs, offset=0)
    except HTTPException as exc:
        # ONLY the timeline-unavailable 502 degrades to null. The other raise
        # reachable from there is the paginated-read 400, unreachable behind
        # the literal offset=0 — and anything a future guard adds must surface
        # rather than read as "this session has no messages".
        if exc.status_code != 502:
            raise
        return None
    return {"session_id": tid, "messages": msgs}


@router.get("/sessions")
async def list_sessions(
    project_id: str,
    document_id: str,
    is_note: bool | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    with_active_messages: bool = False,
    preferred_session_id: str | None = None,
    user: dict = Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    """List chat sessions for current user in a project.

    Visibility:
      - AI chats (is_note None/False): PROJECT-WIDE per (project, user), listed
        as THREADS — the candidates are root rows (a pre-migration row with no
        thread_id is its own thread), each item carrying its active_branch_id
        session's fields (the last-opened branch); limit/offset count THREADS.
        Every AI thread the user owns in the project is visible from ANY
        document — the open document/reference only affects ghost-attach + the
        resolver's active-chat pick, never this list. Privacy holds
        automatically: AI chats carry user_id = $uid + owner-only access. The
        candidate set is capped at MAX_LISTED_SESSIONS newest-by-updated_at
        BEFORE the branch swap, so branches never eat the cap.
      - Note-chats (is_note True): KEEP the branch scope (ancestor/sibling/
        reference-children). Notes are shared document-anchored comment threads
        with a different ACL profile (visible to any commentator+ member); they
        stay scoped so a document sees its own + inherited notes only.

    `document_id` stays required — it gates access (get_document_access) and is the
    resolver's pick scope; it is not a visibility filter for AI chats.

    Ordering is by LAST-MESSAGE time (derived read-time), not chat_sessions.updated_at
    (see chat-sort-by-last-message). ORDER BY updated_at DESC here is only the
    candidate selection key + a deterministic Python-sort tiebreak.
    """
    access = await get_document_access(document_id, user)
    if access is None:
        raise HTTPException(status_code=404, detail="Project not found")

    rows = await _load_candidate_rows(
        db, project_id=project_id, user=user,
        document_id=document_id, is_note=is_note,
    )
    # ARCH: notes have no threads — the display swap is the AI path's. The
    # root id maps every display row back to its thread (the piggyback hint
    # may name a pre-change saved ROOT id as well as the branch it previews).
    if is_note is True:
        display_rows = list(rows or [])
        root_by_row = {extract_id(r.get("id")): extract_id(r.get("id")) for r in display_rows}
    else:
        pairs = await _thread_display_rows(db, rows)
        display_rows = [r for _, r in pairs]
        root_by_row = {extract_id(r.get("id")): root for root, r in pairs}

    # Build ref_map: which document_ids belong to reference-documents.
    # ARCH: Skip context_document_ids for note sessions (notes never use context).
    doc_ids: set[str] = set()
    for r in display_rows:
        did = r.get("document_id")
        if did:
            doc_ids.add(str(did))
        if not r.get("is_note"):
            for cid in (r.get("context_document_ids") or []):
                doc_ids.add(str(cid))
    ref_map = await build_ref_map(db, *doc_ids)

    document_titles = await _document_titles(db, display_rows, ref_map)

    note_ids = [extract_id(r.get("id")) for r in display_rows if r.get("is_note") is True]
    note_ids = [i for i in note_ids if i]
    previews_by_session_id = await _note_previews(db, note_ids)

    ai_ids = [extract_id(r.get("id")) for r in display_rows if r.get("is_note") is not True]
    ai_ids = [i for i in ai_ids if i]
    last_at_by_session = await _last_activity_map(db, ai_ids, previews_by_session_id)

    # Sort by last activity DESC; empty chats fall back to updated_at (then
    # created_at). Python's sorted is stable, so equal keys keep the SQL
    # ORDER BY updated_at DESC tiebreak order.
    sorted_rows = sorted(
        display_rows,
        key=lambda r: (
            last_at_by_session.get(extract_id(r.get("id")))
            or r.get("updated_at") or r.get("created_at")
        ),
        reverse=True,
    )
    page = sorted_rows[offset:offset + limit]
    serialized = [
        serialize_session(
            r, ref_map, previews_by_session_id, last_at_by_session, document_titles,
            viewer_id=user["user_id"],
        )
        for r in page
    ]
    if not with_active_messages:
        return serialized

    # ARCH: collapse the sessions→messages waterfall. The client
    # opts in (with_active_messages) and hints which session it will make active
    # (preferred_session_id = its per-document saved id). We piggyback that
    # session's messages so the panel paints in ONE round-trip instead of two.
    # There is no empty-hint (page[0]) fallback: with project-wide listing, page[0]
    # is the newest chat ANYWHERE in the project rather than the resolver's
    # doc-owned pick_first, so piggybacking it would almost never match. A valid preferred id still
    # piggybacks; the cold/stale path (no id / id outside the capped page) yields
    # active_messages=NULL and the client does a normal second loadMessages.
    # The hint matches the DISPLAY row (a thread's active branch) OR its root —
    # a pre-change saved id still names the thread the display row previews.
    target_row = None
    if preferred_session_id:
        for r in page:
            rid = extract_id(r.get("id"))
            if rid == preferred_session_id or root_by_row.get(rid) == preferred_session_id:
                target_row = r
                break

    active_messages = (
        None if target_row is None else await _piggyback_messages(db, target_row)
    )
    return {"sessions": serialized, "active_messages": active_messages}
