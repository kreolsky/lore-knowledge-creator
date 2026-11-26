"""Chat memory read routes: the chat's table of contents, the memory and the recall it received, and the project's memory-ON personas.

WHY the session guard is the whole access check on both: a synopsis restates
text this reader already receives from list_messages under the same guard, and
the memory titles come from `visible_sessions` as seen from this chat — the
same wall the agent's block was built behind (see ARCH in chat_memory.py).
"""
from agent_config_load import instance_skill_disabled
from chat_memory import visible_for_chat
from chat_sessions.recall import CHAT_RECALL_SKILL
from fastapi import Depends
from surrealdb import AsyncSurreal

from access import require_project_read
from auth import get_current_user
from db import get_db
from jobs.tasks.chat_topics import memory_personas
from routes.chat._router import router
from routes.chat.sessions import _require_session_access


async def _no_toc_reason(db, session: dict) -> str:
    """Why the labeller has written no toc for this chat (first match wins)."""
    if await instance_skill_disabled(db, CHAT_RECALL_SKILL):
        return "recall_off"
    if session.get("unindexed"):
        return "unindexed"
    # Unstamped (no turn yet) ⇒ the persona the first turn will stamp.
    persona = session.get("origin_persona_id")
    if persona is None:
        persona = session.get("system_prompt_id") or ""
    if persona not in await memory_personas(db, session["project_id"]):
        return "memory_off"
    return "pending"


@router.get("/sessions/{session_id}/toc")
async def chat_toc(session_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """The chat's table of contents for the prompt navigator: its topics and
    the user-message ids they cover, or `{"toc": null, "reason": …}` —
    `recall_off` | `unindexed` | `memory_off` | `pending`. A stored toc is
    returned whatever the reason would be: topics are kept forever.
    """
    session = await _require_session_access(session_id, user)
    rows = await db.query(
        "SELECT topics, labelled_ids FROM type::record('chat_tocs', $sid)",
        {"sid": session_id},
    ) or []
    if not rows:
        return {"toc": None, "reason": await _no_toc_reason(db, session)}
    topics = [
        {"title": t["title"], "synopsis": t["synopsis"], "message_ids": t["message_ids"]}
        for t in rows[0].get("topics") or []
    ]
    return {"toc": {"topics": topics, "labelled_ids": rows[0].get("labelled_ids") or []}}


@router.get("/sessions/{session_id}/memory")
async def chat_memory(session_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """The conversation memory and the auto-recall this chat received:
    `{"memory": {message_id, error, chats: [{sid, title, date}], hidden},
    "recall": {message_id, chats: [{sid, title, date}], hidden}}` — a record is
    null when none was written.

    A recorded chat that `visible_sessions` no longer returns (deleted,
    unindexed, under a hidden subtree) is left out and counted in `hidden`.
    """
    session = await _require_session_access(session_id, user)
    memory, recall = session.get("memory"), session.get("recall")
    if not memory and not recall:
        return {"memory": None, "recall": None}
    visible, _ = await visible_for_chat(
        db, session_id, session, user["user_id"], session["project_id"])
    out = {"memory": _received(memory, visible), "recall": _received(recall, visible)}
    # Why memory is on or off, as stored — a recall record is written only with
    # hits, so it never carries one.
    if out["memory"] is not None:
        out["memory"]["error"] = (memory or {}).get("error")
    return out


def _received(record: dict | None, visible: dict) -> dict | None:
    """A stored record as the client reads it: `None` when there is none, else
    its message, the recorded chats whose titles the visible chats still give,
    and how many they no longer do."""
    if not record:
        return None
    recorded = record.get("chats") or []
    chats = [{"sid": c["sid"], "title": visible[c["sid"]]["title"], "date": c["date"]}
             for c in recorded if c["sid"] in visible]
    return {"message_id": record.get("message_id"),
            "chats": chats, "hidden": len(recorded) - len(chats)}


@router.get("/memory-personas")
async def chat_memory_personas(project_id: str, user: dict = Depends(get_current_user), db: AsyncSurreal = Depends(get_db)):
    """`{"persona_ids": [...]}` — the project's personas with conversation memory
    ON, for the persona picker's mark. WHY a route: the frontmatter that decides
    it is parsed server-side only; the client has the personas' titles.
    """
    await require_project_read(project_id, user)
    return {"persona_ids": sorted(await memory_personas(db, project_id))}
