"""What one digest run reads: the cursor, the window, and the new material inside it."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chat_sessions.authors import author_display, is_user_author
from chat_sessions.conversation import origin_of
from chat_sessions.recall_search import (
    Window,
    live_rows,
    search_chats,
    visible_sessions,
)

from db import fetch_many, get_db
from jobs.tasks.chat_index import indexing_state, plan_chat_index
from models import resolve_message_author

# Char budgets per material section, in prompt order; a section stops at its budget.
REFERENCE_CHARS = 24_000
ANCHORED_CHARS = 24_000
RELATED_CHARS = 12_000
PREVIOUS_REPORT_CHARS = 12_000

PENDING_REFERENCE_STATUSES = ("queued", "processing")
# How long a not-yet-indexed chat message may hold the window: the hourly index sweep
# reaches anything a trigger missed within it, so a message older than this is stuck,
# and holding for it would stop the digest for good.
CHAT_INDEX_HOLD = timedelta(hours=1)
ASSISTANT_LABEL = "Assistant"

_MESSAGE_FIELDS = (
    "meta::id(id) AS id, chat_id, role, content, created_at, parent_id, author_id, "
    "author_name, origin_id"
)


@dataclass
class Material:
    references: list[str] = field(default_factory=list)
    anchored: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.references or self.anchored or self.related)


async def previous_report(db, config_id: str, target_doc_id: str) -> dict | None:
    """The live report of this binding with the latest `digest_covered_until`."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, content, digest_covered_until FROM documents "
        "WHERE parent_id = $target AND digest_config_id = $cid AND deleted_at IS NONE "
        "AND digest_covered_until IS NOT NONE ORDER BY digest_covered_until DESC LIMIT 1",
        {"target": target_doc_id, "cid": config_id},
    ) or []
    return rows[0] if rows else None


async def window_end(
    db, theme_doc_id: str, subjects: tuple[str, ...], project_id: str,
    since: datetime | None, now: datetime,
) -> datetime:
    """`now`, or just before the earliest material that exists but cannot be read yet:
    an attached reference still being processed, or a subject's chat message the chat
    index has not reached (younger than CHAT_INDEX_HOLD).

    WHY: a reference is created on upload and filled by transcription later, and a
    message is searchable only once indexed; a window closed over either marks it
    covered and no later run would read it. Holding the window short of it lets the
    run after its transcription / indexing include it.
    """
    held = [now, *await _pending_reference_starts(db, theme_doc_id, since),
            *await _unindexed_message_starts(db, theme_doc_id, subjects, project_id, since, now)]
    earliest = min(held)
    return now if earliest == now else earliest - timedelta(microseconds=1)


async def _pending_reference_starts(db, theme_doc_id: str, since: datetime | None) -> list[datetime]:
    rows = await db.query(
        "SELECT created_at FROM documents WHERE parent_id = $theme AND is_reference = true "
        "AND deleted_at IS NONE AND processing_status IN $pending "
        "AND ($since IS NONE OR created_at > $since) ORDER BY created_at LIMIT 1",
        {"theme": theme_doc_id, "pending": list(PENDING_REFERENCE_STATUSES), "since": since},
    ) or []
    return [r["created_at"] for r in rows]


async def _unindexed_message_starts(
    db, theme_doc_id: str, subjects: tuple[str, ...], project_id: str,
    since: datetime | None, now: datetime,
) -> list[datetime]:
    """Creation times of the subjects' messages the index has not reached yet, inside
    the hold age, outside the chats on the theme document (those are read directly,
    not through the index). Indexing off ⇒ none: nothing would ever reach them."""
    _state, model = await indexing_state(db)
    if model is None:
        return []
    floor = now - CHAT_INDEX_HOLD
    if since is not None:
        floor = max(floor, since)
    starts = []
    for uid in subjects:
        anchored = await visible_sessions(db, uid, project_id, set(), {theme_doc_id})
        pending, _stale = await plan_chat_index(db, uid, project_id, model)
        starts += [m["created_at"] for m in pending
                   if m["created_at"] > floor and m["chat_id"] not in anchored]
    return starts


def _take(blocks: list[str], budget: int) -> list[str]:
    """Blocks in order until `budget` chars; the block that crosses it is cut to fit."""
    kept: list[str] = []
    left = budget
    for block in blocks:
        if left <= 0:
            break
        kept.append(block[:left])
        left -= len(block)
    return kept


async def _references(db, theme_doc_id: str, window: Window) -> list[str]:
    rows = await db.query(
        "SELECT title, content, created_at, created_by FROM documents WHERE parent_id = $theme "
        "AND is_reference = true AND deleted_at IS NONE "
        "AND ($since IS NONE OR created_at > $since) AND created_at <= $until "
        "ORDER BY created_at",
        {"theme": theme_doc_id, "since": window.since, "until": window.until},
    ) or []
    rows = [r for r in rows if (r.get("content") or "").strip()]
    # WHY: the uploader and the date head the block, as an author heads a chat line — a
    # voice diary has no other name in it, and the model is asked to attribute facts.
    names = await _author_names({r["created_by"] for r in rows if r.get("created_by")})
    blocks = [f"### {_reference_heading(r, names)}\n\n{r['content'].strip()}" for r in rows]
    return _take(blocks, REFERENCE_CHARS)


def _reference_heading(row: dict, names: dict[str, str]) -> str:
    parts = [names.get(row.get("created_by") or ""), _iso_date(row.get("created_at"))]
    signed = ", ".join(p for p in parts if p)
    title = row.get("title") or "Reference"
    return f"{title} — {signed}" if signed else title


def _iso_date(value: object) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else ""


async def _author_names(user_ids: set[str]) -> dict[str, str]:
    user_ids = {u for u in user_ids if is_user_author(u)}
    users = await fetch_many("users", sorted(user_ids)) if user_ids else {}
    return {uid: (u or {}).get("name") or uid for uid, u in users.items()}


def _line(message: dict, owner_of: dict[str, str], names: dict[str, str]) -> str:
    if message["role"] == "assistant":
        label = ASSISTANT_LABEL
    else:
        session = {"user_id": owner_of[message["chat_id"]]}
        label = author_display(message, session, names) or resolve_message_author(message, session)
    return f"**{label}:** {message['content'].strip()}"


async def _anchored_messages(db, sessions: dict, window: Window) -> list[dict]:
    """Messages of `sessions` inside the window, oldest first, one per origin."""
    if not sessions:
        return []
    rows = await db.query(
        f"SELECT {_MESSAGE_FIELDS} FROM messages WHERE chat_id IN $sids AND deleted_at IS NONE "
        "AND ($since IS NONE OR created_at > $since) AND created_at <= $until "
        "ORDER BY created_at",
        {"sids": sorted(sessions), "since": window.since, "until": window.until},
    ) or []
    seen: set[str] = set()
    picked = []
    for row in rows:
        origin = origin_of(row)
        if origin not in seen:
            seen.add(origin)
            picked.append(row)
    return picked


async def _neighbours(db, hits: list[dict], sessions: dict) -> dict[str, dict]:
    """`{hit message id: neighbour message}` — an answer's question, a question's answer."""
    parents = await live_rows(db, {h["parent_id"] for h in hits
                                   if h["role"] == "assistant" and h.get("parent_id")}, sessions)
    asked = sorted(h["id"] for h in hits if h["role"] == "user")
    children = []
    if asked:
        children = await db.query(
            f"SELECT {_MESSAGE_FIELDS} FROM messages WHERE parent_id IN $ids AND role = 'assistant' "
            "AND deleted_at IS NONE AND chat_id IN $sids ORDER BY created_at",
            {"ids": asked, "sids": sorted(sessions)},
        ) or []
    out = {h["id"]: parents[h["parent_id"]] for h in hits
           if h["role"] == "assistant" and h.get("parent_id") in parents}
    for child in children:
        out.setdefault(child["parent_id"], child)
    return out


async def _related_turns(
    db, uid: str, project_id: str, query: str, anchored: dict, window: Window,
) -> list[list[dict]]:
    """The subject's best-matching messages outside the theme's chats, each with its
    neighbour turn, in conversation order."""
    result = await search_chats(
        db, uid, project_id, query, exclude=set(anchored), scope="project",
        anchor_doc_id=None, under_document_id=None, window=window,
    )
    if not result["hits"]:
        return []
    sessions = await visible_sessions(db, uid, project_id, set(anchored))
    hits = list((await live_rows(db, {h["message_id"] for h in result["hits"]}, sessions)).values())
    by_score = {h["message_id"]: i for i, h in enumerate(result["hits"])}
    hits.sort(key=lambda m: by_score[m["id"]])
    near = await _neighbours(db, hits, sessions)
    turns = []
    for hit in hits:
        neighbour = near.get(hit["id"])
        if neighbour is None:
            turns.append([hit])
        elif hit["role"] == "assistant":
            turns.append([neighbour, hit])
        else:
            turns.append([hit, neighbour])
    return turns


async def collect(
    project_id: str, theme_doc_id: str, subjects: tuple[str, ...], query: str, window: Window,
) -> Material:
    """Everything new in `window`: references on the theme document, the subjects'
    chats anchored on it, and their other chats that match `query`."""
    db = await get_db()
    material = Material(references=await _references(db, theme_doc_id, window))
    owner_of: dict[str, str] = {}
    anchored_msgs: list[dict] = []
    related_turns: list[list[dict]] = []
    for uid in subjects:
        anchored = await visible_sessions(db, uid, project_id, set(), {theme_doc_id})
        owner_of.update({sid: uid for sid in anchored})
        anchored_msgs += await _anchored_messages(db, anchored, window)
        for turn in await _related_turns(db, uid, project_id, query, anchored, window):
            owner_of.update({m["chat_id"]: uid for m in turn})
            related_turns.append(turn)
    names = await _author_names(
        set(subjects) | {m["author_id"] for m in anchored_msgs if m.get("author_id")}
        | {m["author_id"] for t in related_turns for m in t if m.get("author_id")})
    anchored_msgs.sort(key=lambda m: m["created_at"])
    material.anchored = _take([_line(m, owner_of, names) for m in anchored_msgs], ANCHORED_CHARS)
    seen: set[str] = set()
    blocks = []
    for turn in related_turns:
        if origin_of(turn[-1]) in seen:
            continue
        seen.add(origin_of(turn[-1]))
        blocks.append("\n".join(_line(m, owner_of, names) for m in turn))
    material.related = _take(blocks, RELATED_CHARS)
    return material
