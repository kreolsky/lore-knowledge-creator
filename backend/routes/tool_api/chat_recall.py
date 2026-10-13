"""Tool-API — chat recall (`search_my_chats`): the caller's own other chats of the project.

The search half and its live-row predicate live in chat_sessions.recall_search,
shared with the person's own chat-search route; this handler adds the key auth,
the off-switch, the calling conversation's exclusion and anchor, and the window read.
"""
from agent.context import get_agent_context, owned_session_row
from chat_sessions.conversation import conversation_session_ids
from chat_sessions.recall import chat_recall_disabled
from chat_sessions.recall_search import (
    MESSAGE_FIELDS,
    iso,
    live_rows,
    root_documents,
    search_chats,
    visible_sessions,
)
from fastapi import Body, Depends, HTTPException

from db import get_db
from models import ToolSearchMyChats
from routes.tool_api_telemetry import track_agent_tool

CHAT_RECALL_WINDOW_CHARS = 8000


@track_agent_tool("search_my_chats")
async def tool_search_my_chats(
    body: ToolSearchMyChats = Body(...),
    ctx: dict = Depends(get_agent_context),
):
    """Search the caller's other chats of the key's project, or read the turns
    around one hit. Refused while the admin disabled the `chat-recall` skill —
    the route is reachable by a key directly, so the skill tombstone alone does
    not stop it."""
    db = await get_db()
    if await chat_recall_disabled(db):
        raise HTTPException(status_code=403,
                            detail="chat-recall is disabled by the administrator")
    user_id, project_id = ctx["user"]["user_id"], ctx["project_id"]
    exclude, anchor = await _current_conversation(db, ctx)
    if body.message_id is not None:
        sessions = await visible_sessions(db, user_id, project_id, exclude)
        return await _read_window(db, sessions, body.message_id, body.offset)
    return await search_chats(
        db, user_id, project_id, body.query, exclude=exclude, scope=body.scope,
        anchor_doc_id=anchor, under_document_id=body.under_document_id)


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


async def _session_chain(db, chat_id: str) -> list[dict]:
    """The session's messages in parent_id order. Deleted rows are walked (a
    deleted row never splits the chain) and dropped from the result."""
    rows = await db.query(
        f"SELECT {MESSAGE_FIELDS}, deleted_at FROM messages WHERE chat_id = $cid",
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
