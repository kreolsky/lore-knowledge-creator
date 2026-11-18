"""Conversation memory — the block of a persona's chats with this user, built until the chat has received memory.

Order: by last message, newest first; a persona with `chat_memory_order: document`
puts the chats under the current document's subtree first.

# ARCH: the block is built ONLY from `visible_sessions` (see SYSTEM: chat-recall,
# chat_sessions/recall_search.py) — the security function is called unchanged,
# and every later read (origin persona, chat_tocs, last message) runs over the
# session ids it returned, never as a selection of its own. The plugin injects
# the block as a `lore-memory` context message once per active surface (see
# ARCH in harness-driver/plugin/src/preload.ts); once a chat's record says it
# received chats (chat_sessions.memory) it is never rebuilt, so labelling
# another chat cannot void the provider's cache of the history.
"""
import logging

from chat_sessions.conversation import (
    conversation_session_ids,
    last_message_at,
    newest_branch_per_thread,
)
from chat_sessions.recall_search import root_documents, visible_sessions

from config import count_tokens_approx
from scope import subtree_doc_ids

logger = logging.getLogger(__name__)

HEADER = (
    "# Recent conversations with this user\n"
    "These are topics of the user's earlier chats with you, not facts — open the details "
    "with `search_my_chats` (message_id=…) or `read_my_chat`.\n"
    "The list is cut to a budget and omits older and not-yet-labelled chats — when the user "
    "refers to a conversation not listed here, find it with `search_my_chats` or `list_my_chats`."
)


def _chat_text(chat: dict) -> str:
    lines = [f"{chat['date']} · {chat['title'] or 'Untitled'} (chat {chat['sid']})"]
    for topic in chat["topics"]:
        first = (topic.get("message_ids") or [""])[0]
        lines.append(f"- {topic['title']} — {topic['synopsis']} (from message {first})")
    return "\n".join(lines)


def render_block(chats: list[dict], budget: int) -> tuple[str, list[dict]]:
    """The block for `chats` (already ordered) and the chats it includes: each
    chat WHOLE, in order, stopping right after the chat that brings the block
    to `budget` tokens; ("", []) only for an empty list.

    WHY whole chats and a budget that is overshot: operator ruling — nothing
    is skipped, or a piece falls out of the thread of the conversation; the
    first chat always goes in whole, and the HEADER tells the agent to search
    for what did not fit.
    """
    if not chats:
        return "", []
    parts = [HEADER]
    shown: list[dict] = []
    used = count_tokens_approx(HEADER)
    for chat in chats:
        text = _chat_text(chat)
        parts.append(text)
        shown.append(chat)
        used += count_tokens_approx("\n\n" + text)
        if used >= budget:
            break
    return "\n\n".join(parts), shown


async def _persona_candidates(
    db, visible: dict[str, dict], user_id: str, project_id: str, persona_id: str,
) -> list[dict]:
    """The visible sessions bound to `persona_id` (by default the start persona;
    the owner may re-bind), with their topics and last message time — every
    read runs over `visible`'s ids only (INVARIANT in build_memory_block)."""
    meta = await db.query(
        "SELECT meta::id(id) AS sid, origin_persona_id "
        "FROM $ids.map(|$x| type::record('chat_sessions', $x))",
        {"ids": sorted(visible)},
    ) or []
    mine = {m["sid"]: m for m in meta if m.get("origin_persona_id") == persona_id}
    if not mine:
        return []
    tocs = await db.query(
        "SELECT chat_id, topics FROM chat_tocs WHERE user_id = $uid "
        "AND project_id = $pid AND chat_id IN $ids",
        {"uid": user_id, "pid": project_id, "ids": sorted(mine)},
    ) or []
    topics = {t["chat_id"]: t.get("topics") or [] for t in tocs}
    last_at = await last_message_at(db, sorted(mine))
    return [
        {"sid": sid, "root": visible[sid]["root"], "title": visible[sid]["title"],
         "document_id": visible[sid]["document_id"], "topics": topics.get(sid, []),
         "created_at": visible[sid]["created_at"], "last_at": last_at.get(sid)}
        for sid in mine
    ]


async def visible_for_chat(
    db, session_id: str, session: dict, user_id: str, project_id: str,
) -> tuple[dict[str, dict], str | None]:
    """`visible_sessions` as seen FROM this chat — its own conversation
    excluded, the viewer being its root document — and that document id."""
    own = await conversation_session_ids(db, session_id, session)
    root = session.get("thread_id") or session_id
    viewer_doc = (await root_documents(db, {root})).get(root)
    visible = await visible_sessions(
        db, user_id, project_id, exclude=own, viewer_doc_id=viewer_doc)
    return visible, viewer_doc


async def build_memory_block(
    db, *, session_id: str, session: dict, user_id: str, project_id: str,
    persona_id: str, budget: int, order: str,
) -> tuple[str, list[dict]]:
    """The persona's conversation-memory block for this chat and the chats it
    shows as `[{sid, date}]` (("", []) = nothing to show).

    Chats come from `visible_sessions` minus this conversation, keep those that
    are BOUND to `persona_id` (by default the start persona; the owner may
    re-bind), one per thread — its newest branch (newest_branch_per_thread,
    the labeller's pick) — and only those with topics, by last message newest
    first; `order="document"` puts the ones under the current document's
    subtree first.
    """
    # INVARIANT(security): the candidate set is exactly visible_sessions' output;
    # the queries below read only the session ids it returned.
    # Why: visible_sessions is the one wall between members and around deleted,
    # unindexed and hidden-subtree chats — a direct chat_tocs selection would
    # bypass it, and a synopsis restates the chat's text.
    visible, viewer_doc = await visible_for_chat(db, session_id, session, user_id, project_id)
    if not visible:
        logger.info("chat memory block chat=%s persona=%s: no other visible chats", session_id, persona_id)
        return "", []
    candidates = await _persona_candidates(db, visible, user_id, project_id, persona_id)
    if not candidates:
        logger.info("chat memory block chat=%s persona=%s: visible=%d, none bound to this persona",
                    session_id, persona_id, len(visible))
        return "", []
    last_at = {c["sid"]: c["last_at"] for c in candidates if c["last_at"]}
    picked = newest_branch_per_thread(candidates, last_at)
    chats = sorted((c for c in picked if c["topics"]), key=lambda c: c["last_at"], reverse=True)
    if order == "document" and viewer_doc:
        subtree = set(await subtree_doc_ids(viewer_doc, project_id))
        chats.sort(key=lambda c: c["document_id"] not in subtree)  # stable: subtree first
    for chat in chats:
        chat["date"] = chat["last_at"].date().isoformat()
    block, shown = render_block(chats, budget)
    logger.info(
        "chat memory block chat=%s persona=%s candidates=%d labelled=%d tokens=%d",
        session_id, persona_id, len(picked), len(chats), count_tokens_approx(block),
    )
    return block, [{"sid": c["sid"], "date": c["date"]} for c in shown]
