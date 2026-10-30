"""Tool-API — chat recall: the caller's own other chats of the project.

The search half and its live-row predicate live in chat_sessions.recall_search,
shared with the person's own chat-search route; this handler adds the key auth,
the off-switch, the calling conversation's exclusion and anchor, the window read,
and the skill pack's list (whole-chat find), read (whole-chat paging) and
view_chat_image (one attached image) tools over the SAME predicate.
"""
import re
from typing import NamedTuple

from agent.context import get_agent_context, owned_session_row
from chat_sessions.conversation import conversation_session_ids
from chat_sessions.recall import chat_recall_disabled
from chat_sessions.recall_search import (
    MESSAGE_FIELDS,
    iso,
    live_rows,
    require_scope_document,
    root_documents,
    search_chats,
    visible_sessions,
)
from fastapi import Body, Depends, HTTPException

from db import get_db
from models import (
    ToolListMyChats,
    ToolReadMyChat,
    ToolSearchMyChats,
    ToolViewChatImage,
)
from routes.tool_api_telemetry import track_agent_tool
from scope import subtree_doc_ids

CHAT_RECALL_WINDOW_CHARS = 8000
CHAT_READ_PAGE_CHARS = 30000
# The per-image view cap, in DECODED bytes — the same figure the driver's image
# admission uses (dsh DEFAULT_MAX_IMAGE_BYTES, the min read_image honours), so
# what this route serves always fits what saveImage will accept.
VIEW_CHAT_IMAGE_MAX_BYTES = 20 * 1024 * 1024


def _require_in_app_agent(ctx: dict) -> None:
    """The chat-recall pack is closed to EXTERNAL keys — the person's chat
    corpus is the in-app assistant's own memory, not a surface for
    user-minted automation or MCP-gateway clients. Internal keys
    only: the driver's standing per-session keys AND its scoped run keys are
    both internal=True (SYSTEM: agent-keys). Same predicate family as the
    sandbox gate (routes/tool_api/sandbox/files.py) — explicit True only.
    Why a 403 and not a 404: the caller is authenticated and this is a policy
    statement about the surface, not an existence claim about any chat."""
    if ctx.get("internal") is not True:
        raise HTTPException(
            status_code=403,
            detail="chat-recall is available only to the in-app assistant",
        )


@track_agent_tool("search_my_chats")
async def tool_search_my_chats(
    body: ToolSearchMyChats = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Search the caller's other chats of the key's project, or read the turns
    around one hit. Refused while the admin disabled the `chat-recall` skill —
    the route is reachable by a key directly, so the skill tombstone alone does
    not stop it."""
    _require_in_app_agent(ctx)
    db = await get_db()
    if await chat_recall_disabled(db):
        raise HTTPException(status_code=403,
                            detail="chat-recall is disabled by the administrator")
    user_id, project_id = ctx["user"]["user_id"], ctx["project_id"]
    exclude, anchor = await _current_conversation(db, ctx)
    if body.message_id is not None:
        sessions = await visible_sessions(db, user_id, project_id, exclude,
                                          viewer_doc_id=anchor)
        return await _read_window(db, sessions, body.message_id, body.offset)
    return await search_chats(
        db, user_id, project_id, body.query, exclude=exclude, scope=body.scope,
        anchor_doc_id=anchor, under_document_id=body.under_document_id,
        viewer_doc_id=anchor)


async def _current_conversation(db, ctx: dict) -> tuple[set[str], str | None]:
    """`(every session of the calling conversation, its scope document)`, or
    `(empty, None)` when there is none.

    WHY: the exclusion is a relevance filter, not a wall — an absent header or a
    session the caller does not own runs the search unexcluded instead of
    refusing; the header can only narrow the result, never widen it.
    """
    if not ctx.get("session_id"):
        return set(), None
    session = await owned_session_row(ctx)
    if not session:
        return set(), None
    root = session.get("thread_id") or ctx["session_id"]
    anchor = (await root_documents(db, {root})).get(root)
    return await conversation_session_ids(db, ctx["session_id"], session), anchor


# ─── window read ─────────────────────────────────────────────────────────────


def _image_marker(m: dict) -> dict:
    """The `images: N` marker for one served message — a COUNT, never bytes or
    URLs: the text results stay cheap and the agent learns an image exists
    without seeing it (the pixels are asked for with `view_chat_image`)."""
    n = m.get("n_images") or 0
    return {"images": n} if n > 0 else {}


async def _session_chain(db, chat_id: str) -> list[dict]:
    """The session's messages in parent_id order. Deleted rows are walked (a
    deleted row never splits the chain) and dropped from the result. Image
    COUNTS ride along (`array::len`, computed in the DB so a data-URI payload
    never enters a text read) for the `images: N` marker."""
    rows = await db.query(
        f"SELECT {MESSAGE_FIELDS}, deleted_at, "
        "IF images IS NONE THEN 0 ELSE array::len(images) END AS n_images "
        "FROM messages WHERE chat_id = $cid",
        {"cid": chat_id},
    ) or []
    ids = {r["id"] for r in rows}
    children: dict[str | None, list[dict]] = {}
    for r in rows:
        key = r.get("parent_id") if r.get("parent_id") in ids else None
        children.setdefault(key, []).append(r)
    ordered: list[dict] = []
    frontier = sorted(children.get(None, []), key=lambda r: iso(r.get("created_at")))
    seen: set[str] = set()
    while frontier:
        row = frontier.pop(0)
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        ordered.append(row)
        kids = sorted(children.get(row["id"], []), key=lambda r: iso(r.get("created_at")))
        frontier = kids + frontier
    return [r for r in ordered if r.get("deleted_at") is None]


def _turns(chain: list[dict]) -> list[list[dict]]:
    """Group a chain into turns: each user message opens one."""
    turns: list[list[dict]] = []
    for row in chain:
        if row["role"] == "user" or not turns:
            turns.append([row])
        else:
            turns[-1].append(row)
    return turns


def _window_messages(window: list[dict], hit_id: str, offset: int) -> tuple[list[dict], int | None]:
    """The window's messages within CHAT_RECALL_WINDOW_CHARS, and the hit's
    next_offset while it continues. The hit is served first; a hit over the
    budget (or a paging call, offset > 0) is served alone."""
    hit = next(m for m in window if m["id"] == hit_id)
    end = offset + CHAT_RECALL_WINDOW_CHARS
    served = {hit_id: hit["content"][offset:end]}
    next_offset = end if len(hit["content"]) > end else None
    remaining = 0 if offset or next_offset else CHAT_RECALL_WINDOW_CHARS - len(served[hit_id])
    pos = [m["id"] for m in window].index(hit_id)
    for i in sorted(range(len(window)), key=lambda i: abs(i - pos)):
        m = window[i]
        if m["id"] == hit_id or remaining <= 0:
            continue
        served[m["id"]] = m["content"][:remaining]
        remaining -= len(served[m["id"]])
    out = [{
        "message_id": m["id"], "role": m["role"], "created_at": iso(m.get("created_at")),
        "content": served[m["id"]],
        **({"truncated": True} if len(served[m["id"]]) < len(m["content"]) else {}),
        **_image_marker(m),
    } for m in window if m["id"] in served]
    return out, next_offset


async def _read_window(db, sessions: dict, message_id: str, offset: int) -> dict:
    """The hit's turn plus one turn above and below. A message outside the
    caller's visible sessions is a uniform 404 — no existence oracle."""
    hit = (await live_rows(db, {message_id}, sessions)).get(message_id)
    if not hit:
        raise HTTPException(status_code=404, detail="message not found")
    turns = _turns(await _session_chain(db, hit["chat_id"]))
    i = next(n for n, t in enumerate(turns) if any(m["id"] == message_id for m in t))
    window = [m for t in turns[max(0, i - 1):i + 2] for m in t]
    messages, next_offset = _window_messages(window, message_id, offset)
    result: dict = {
        "chat_id": hit["chat_id"], "chat_title": sessions[hit["chat_id"]]["title"],
        "hit_message_id": message_id, "total_chars": len(hit["content"]),
        "messages": messages,
    }
    if i >= 2:
        result["earlier"] = turns[i - 2][0]["id"]
    if i + 2 < len(turns):
        result["later"] = turns[i + 2][0]["id"]
    if next_offset is not None:
        result["next_offset"] = next_offset
    return result


# ─── list: the caller's chats, one row each ───────────────────────────────────


@track_agent_tool("list_my_chats")
async def tool_list_my_chats(
    body: ToolListMyChats = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """List the caller's other chats of the key's project — the whole pack, not
    only the indexed ones. Refused while the admin disabled the `chat-recall`
    skill, for the same reason as the search."""
    _require_in_app_agent(ctx)
    db = await get_db()
    if await chat_recall_disabled(db):
        raise HTTPException(status_code=403,
                            detail="chat-recall is disabled by the administrator")
    user_id, project_id = ctx["user"]["user_id"], ctx["project_id"]
    exclude, anchor = await _current_conversation(db, ctx)
    sessions = await _listed_sessions(db, user_id, project_id, exclude,
                                      body.under_document_id, anchor)
    rows = await _list_rows(db, sessions)
    if body.title_contains is not None:
        needle = body.title_contains.lower()
        rows = [r for r in rows if needle in r["title"].lower()]
    rows.sort(key=_recency, reverse=True)
    result: dict = {
        "rows": rows[body.offset:body.offset + body.limit], "total": len(rows),
    }
    if body.offset + body.limit < len(rows):
        result["next_offset"] = body.offset + body.limit
    return result


async def _listed_sessions(
    db, user_id: str, project_id: str, exclude: set[str], under_document_id: str | None,
    viewer_doc_id: str | None = None,
) -> dict:
    """The list's session set: the shared visible predicate, narrowed to a
    document subtree on request (the same arm as the search's `subtree` scope,
    404 on an unknown / deleted / foreign root included)."""
    if under_document_id is None:
        return await visible_sessions(db, user_id, project_id, exclude,
                                      viewer_doc_id=viewer_doc_id)
    await require_scope_document(project_id, under_document_id)
    under = set(await subtree_doc_ids(under_document_id, project_id))
    return await visible_sessions(db, user_id, project_id, exclude, under,
                                  viewer_doc_id=viewer_doc_id)


async def _list_rows(db, sessions: dict) -> list[dict]:
    """One row per visible session (a branch is its own session and its own
    transcript). Counts and last-message come from ONE grouped query over live
    messages — never one query per session; a session with none keeps its
    `created_at` as `last_message_at`."""
    agg = await db.query(
        "SELECT chat_id, count() AS n, "
        "time::from_unix(math::max(time::unix(created_at))) AS last_at "
        "FROM messages WHERE chat_id IN $sids AND deleted_at IS NONE GROUP BY chat_id",
        {"sids": sorted(sessions)},
    ) or []
    stats = {r["chat_id"]: r for r in agg}
    titles = await _document_titles(db, {s["document_id"] for s in sessions.values()})
    return [{
        "chat_id": sid, "thread_id": s["root"], "title": s["title"],
        "document_id": s["document_id"],
        "document_title": titles.get(s["document_id"]),
        "created_at": iso(s.get("created_at")),
        "last_message_at": iso((stats.get(sid) or {}).get("last_at") or s.get("created_at")),
        "message_count": (stats.get(sid) or {}).get("n", 0),
    } for sid, s in sessions.items()]


async def _document_titles(db, doc_ids) -> dict[str, str]:
    """`{document_id: title}` for the ids that have one. No deleted_at filter —
    a live branch can sit under a dead root, which still has a name."""
    ids = sorted(d for d in doc_ids if d)
    if not ids:
        return {}
    rows = await db.query(
        "SELECT meta::id(id) AS did, title "
        "FROM $ids.map(|$x| type::record('documents', $x))",
        {"ids": ids},
    ) or []
    return {r["did"]: r.get("title") or "" for r in rows if r.get("did")}


def _recency(row: dict) -> tuple[str, str, str]:
    """The list's order: `last_message_at` desc, ties by `created_at` then id
    (ISO strings compare chronologically — same unit the chain walk sorts by)."""
    return (row["last_message_at"], row["created_at"], row["chat_id"])


# ─── read: one chat, pages of whole turns ─────────────────────────────────────


@track_agent_tool("read_my_chat")
async def tool_read_my_chat(
    body: ToolReadMyChat = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Read one of the caller's other chats whole, from its first turn, in pages
    of whole turns — or, with `chat_id` omitted, the caller's CURRENT session:
    the fallback read for messages the prompt no longer carries (chats started
    before the image marker, compacted history). Refused while the admin
    disabled the `chat-recall` skill; a chat outside the visible set is a
    uniform 404 — no existence oracle (an explicit `chat_id` inside the calling
    conversation included: sibling branches are not addressable, only the
    OMISSION reads the current session)."""
    _require_in_app_agent(ctx)
    db = await get_db()
    if await chat_recall_disabled(db):
        raise HTTPException(status_code=403,
                            detail="chat-recall is disabled by the administrator")
    if body.chat_id is None:
        # ARCH: the current-session arm — the ONE read of the calling
        # conversation the pack allows, addressed by OMISSION and not by id:
        # the session is the caller's own (`owned_session_row`, the same
        # ownership predicate every X-Agent-Session-Id consumer uses), so the
        # visible-set wall is meaningless here, while an explicit chat_id
        # inside `exclude` must keep today's 404 (a sibling branch is a
        # different transcript).
        chat_id = ctx.get("session_id") or ""
        session = await owned_session_row(ctx) if chat_id else None
        if not session:
            raise HTTPException(status_code=404, detail="chat not found")
        chat = {"title": session.get("title") or "",
                "document_id": session.get("document_id")}
    else:
        user_id, project_id = ctx["user"]["user_id"], ctx["project_id"]
        exclude, anchor = await _current_conversation(db, ctx)
        chat_id = body.chat_id
        chat = (await visible_sessions(db, user_id, project_id, exclude,
                                       viewer_doc_id=anchor)).get(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="chat not found")
    turns = _turns(await _session_chain(db, chat_id))
    page, next_turn = _read_page(turns, body.from_turn)
    result: dict = {
        "chat_id": chat_id, "chat_title": chat["title"],
        "document_id": chat.get("document_id"), "total_turns": len(turns),
        "turns": page,
    }
    if next_turn is not None:
        result["next_turn"] = next_turn
    return result


def _read_page(
    turns: list[list[dict]], from_turn: int,
) -> tuple[list[list[dict]], int | None]:
    """Whole turns from `from_turn` while they fit CHAT_READ_PAGE_CHARS; the
    first turn is always served, so a page is never empty before the end.
    Returns `(served turns, next from_turn or None)`."""
    page: list[list[dict]] = []
    budget = CHAT_READ_PAGE_CHARS
    i = from_turn
    while i < len(turns):
        turn = turns[i]
        if page and sum(len(m["content"]) for m in turn) > budget:
            break
        served, cut = _serve_turn(turn, budget)
        page.append(served)
        budget -= sum(len(m["content"]) for m in served)
        i += 1
        if cut:
            break
    return page, (i if i < len(turns) else None)


def _serve_turn(turn: list[dict], budget: int) -> tuple[list[dict], bool]:
    """One turn's messages within `budget`. Every message is capped at
    CHAT_READ_PAGE_CHARS (a huge paste before the reply
    is never served whole); the turn's LAST message is additionally capped at
    the page budget still unused. A cut message — any position — carries
    `truncated` + `next_offset` (its tail is read with `search_my_chats`);
    a cut NON-last message does not stop the turn, so the turn's later
    messages are still served and nothing is orphaned. Returns `(served
    rows, the page must stop after this turn)`."""
    served: list[dict] = []
    cut = False
    for i, m in enumerate(turn):
        last = i == len(turn) - 1
        if not last:
            served.append(_message_row(m, m["content"][:CHAT_READ_PAGE_CHARS]))
            if len(m["content"]) > CHAT_READ_PAGE_CHARS:
                cut = True
        elif len(m["content"]) <= budget:
            served.append(_message_row(m, m["content"]))
        else:
            piece = m["content"][:budget] if budget > 0 else ""
            served.append(_message_row(m, piece))
            cut = True
            break
    return served, cut


def _message_row(m: dict, content: str) -> dict:
    """A served message; a cut one is marked `truncated` with `next_offset` (the
    number of its characters served — its rest is read with `search_my_chats`)."""
    row: dict = {
        "message_id": m["id"], "role": m["role"],
        "created_at": iso(m.get("created_at")), "content": content,
        **_image_marker(m),
    }
    if len(content) < len(m["content"]):
        row["truncated"] = True
        row["next_offset"] = len(content)
    return row


# ─── view: one attached image of a visible message ────────────────────────────

# The write-side admission (routes/chat/serializers.py `_validate_image_url`)
# lets in data URIs (`data:image/<subtype>;base64,<payload>`) and HTTPS URLs
# only; this parse mirrors that shape for the SERVED read.
_VIEW_IMAGE_DATA_URI_RE = re.compile(
    r"^data:(image/[a-z+]+);base64,(.+)$", re.IGNORECASE | re.DOTALL)


@track_agent_tool("view_chat_image")
async def tool_view_chat_image(
    body: ToolViewChatImage = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """One image attached to a message of the caller's other chats — the image
    follows the text under the pack's EXISTING visibility predicate: what
    `read_my_chat` cannot see, this cannot see either (uniform 404, no
    existence oracle). Refused while the admin disabled the `chat-recall` skill,
    like the rest of the pack.

    # INVARIANT(security): admission is the SAME predicate chain as the pack's reads.
    # Why: a byte route behind a weaker gate than the text reads would re-open
    #     what the pack's wall exists to close. The chain lives ONCE in
    #     `admit_chat_message` (own_conversation=False here — the calling
    #     conversation stays today's 404); error details carry no image data
    #     (the proxy forwards `buf.slice(0, 300)` to the model).
    """
    _require_in_app_agent(ctx)
    db = await get_db()
    admitted = await admit_chat_message(db, ctx, body.message_id,
                                        own_conversation=False)
    if body.index >= len(admitted.images):
        raise HTTPException(status_code=404, detail="image not found")
    return _view_image_result(admitted.images[body.index], body, admitted)


def _conversation_sessions(ids: set[str]) -> dict[str, dict]:
    """A sessions-dict SHAPE over the conversation's session ids — `live_rows`
    and `_admitted_images` key on the ids only; the meta is never read on the
    own-conversation arm (import_file needs no title)."""
    return {sid: {"title": "", "root": sid, "document_id": None} for sid in ids}


class AdmittedChatMessage(NamedTuple):
    """One admitted chat message: its live row, its images, its chat title."""

    hit: dict
    images: list[str]
    chat_title: str


async def admit_chat_message(
    db, ctx: dict, message_id: str, *, own_conversation: bool,
) -> AdmittedChatMessage:
    """The pack's ONE admission chain for one chat message and its images —
    `tool_view_chat_image` and `import_file`'s attachment path call THIS,
    never a re-stated copy of the chain.

    `own_conversation=False` (view): the calling conversation stays invisible —
    today's uniform 404. `own_conversation=True` (import): a message of the
    calling conversation (any turn, any branch) is admitted FIRST, without the
    chat-recall off-switch — the prompt's image marker addresses exactly that
    conversation (today's attachment path never consulted the switch either);
    every other message then runs the recall wall verbatim (off-switch →
    `visible_sessions(viewer_doc_id=anchor)` → `live_rows`).

    # INVARIANT(security): the off-switch and the visible-session wall are the
    #     recall pack's whole boundary.
    #     Why: an image read behind a weaker gate than the text reads would
    #     re-open what the wall closes. A message outside the admitted
    #     sessions is a uniform 404 (no existence oracle), a dead message
    #     included: deleting a session soft-deletes its messages first
    #     (chat_sessions.delete), and `live_rows` re-checks `deleted_at IS
    #     NONE` on every read — so a deleted branch of the calling
    #     conversation is a 404 even on the own-conversation arm.
    """
    user_id, project_id = ctx["user"]["user_id"], ctx["project_id"]
    exclude, anchor = await _current_conversation(db, ctx)
    sessions: dict = {}
    hit: dict | None = None
    if own_conversation and exclude:
        sessions = _conversation_sessions(exclude)
        hit = (await live_rows(db, {message_id}, sessions)).get(message_id)
    if hit is None:
        if await chat_recall_disabled(db):
            raise HTTPException(status_code=403,
                                detail="chat-recall is disabled by the administrator")
        sessions = await visible_sessions(db, user_id, project_id, exclude,
                                          viewer_doc_id=anchor)
        hit = (await live_rows(db, {message_id}, sessions)).get(message_id)
    if hit is None:
        raise HTTPException(status_code=404, detail="message not found")
    images = await _admitted_images(db, message_id, sessions)
    return AdmittedChatMessage(
        hit, images, sessions.get(hit["chat_id"], {}).get("title") or "",
    )


async def _admitted_images(db, message_id: str, sessions: dict) -> list[str]:
    """`messages.images` of the ADMITTED message. `live_rows` selects
    MESSAGE_FIELDS only, so the images are re-read here keyed by the admitted
    id, re-stating `deleted_at IS NONE AND chat_id IN $sids`; the images array
    is written only at create (MessageCreate / the driver claim / branch
    copies), so the served marker and this read cannot diverge (no TOCTOU)."""
    rows = await db.query(
        "SELECT images FROM messages "
        "WHERE meta::id(id) = $mid AND deleted_at IS NONE AND chat_id IN $sids",
        {"mid": message_id, "sids": sorted(sessions)},
    ) or []
    return (rows[0].get("images") if rows else None) or []


def _view_image_result(
    url: str, body: ToolViewChatImage, admitted: AdmittedChatMessage,
) -> dict:
    """The served image: a stored data URI as `{image: {media_type, data}}`
    under the view cap; a linked (HTTPS) image answers `unavailable` with NO
    URL — Lore stores the link, never the bytes, never fetches it (SSRF)."""
    if not url.startswith("data:"):
        return {"unavailable": "image is a link, not stored in Lore",
                "chat_id": admitted.hit["chat_id"], "message_id": body.message_id,
                "index": body.index}
    match = _VIEW_IMAGE_DATA_URI_RE.match(url)
    if not match:
        raise HTTPException(status_code=400, detail="image is not a valid data URI")
    media_type, data = match.group(1).lower(), match.group(2)
    # Decoded size (base64 length overstates it ~4/3x): the cap names what the
    # DRIVER will admit, and that side sees decoded bytes.
    if (len(data) - data.count("=")) * 3 // 4 > VIEW_CHAT_IMAGE_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"image is over the {VIEW_CHAT_IMAGE_MAX_BYTES}-byte "
                   f"({VIEW_CHAT_IMAGE_MAX_BYTES // 1024 // 1024} MiB) view limit")
    return {"image": {"media_type": media_type, "data": data},
            "chat_id": admitted.hit["chat_id"], "message_id": body.message_id,
            "index": body.index, "chat_title": admitted.chat_title}
