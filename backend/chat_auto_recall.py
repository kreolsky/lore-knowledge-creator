"""Persona auto-recall — the server itself brings the earlier exchanges closest to what
the person just said into a new chat: no request, no tool call, no model-written query.

# ARCH: beside the persona's conversation-memory block (chat_memory.build_memory_block,
a table of contents by recency) a SECOND block carries the ORIGINALS — the user's words
and the answer — of the exchanges most similar to the user's text of the turn. The
candidates are exactly the memory block's (visible_for_chat → _persona_candidates →
newest_branch_per_thread); the chat index (chat_chunks) only PROPOSES, and every hit is
re-decided against live rows before a word is quoted. A score floor above the search
floor separates "the same subject" from "shared words" (CHAT_RECALL_AUTO_MIN_SCORE,
calibrated — see its help). This module decides WHAT is recalled; the turn path decides
WHEN (a persona with `chat_recall_budget`, the chat row's first messages until one
recall lands) and what a failure costs the turn.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

import settings
from chat_memory import _persona_candidates, visible_for_chat
from chat_sessions.conversation import newest_branch_per_thread
from chat_sessions.recall_search import (
    MESSAGE_FIELDS,
    Window,
    _collapse,
    _score_chunks,
    live_rows,
)
from markdown_chunker import chunk_markdown

import embeddings
from config import count_tokens_approx
from db import parse_iso

logger = logging.getLogger(__name__)

# A message with fewer words than this (punctuation dropped) carries no subject.
GATE_MIN_WORDS = 4
# Query pieces embedded — the user's text cut as the chat index cuts a message; the
# first N, so a pasted page costs one small call, not a document-sized one.
QUERY_MAX_PIECES = 4
# The bound on ONE recall — candidates, embedding, KNN, live reads, rendering. Past it
# the turn goes on without the block (the caller warns): first-token delay grows by at
# most this much. WHY a bound on the whole recall, not on the embedding call alone: the
# KNN and the live reads share the DB connection with the turn's own prep.
RECALL_TIMEOUT_S = 3.0
# The shape: groups (= a topic, else a chat) and exchanges per group.
MAX_GROUPS = 5
MAX_EXCHANGES_PER_GROUP = 2
# A quoted exchange: the user's words, then the answer at half the length — the user's
# words carry the attitude, the answer what was decided or given; the user's dominate.
USER_CHARS = 500
ANSWER_CHARS = 250

HEADER = (
    "# Earlier conversations close to what the user said\n"
    "Excerpts of the user's earlier chats with you on a close subject: the user's own "
    "words and your answer as said then, quoted and clipped. A decision or a preference "
    "here may have changed since. Use what fits naturally and say when "
    "(\"in «<chat title>» on <date> …\"); open the rest of a chat with `read_my_chat` "
    "(chat_id) or `search_my_chats` (message_id=…). What does not fit what the user "
    "means now, ignore."
)


@dataclass(frozen=True)
class RecallResult:
    """What one attempt produced: `block` ("" = nothing to inject — gated, no
    candidates or nothing above the floor), the chats it quotes as `[{sid, date}]`
    (the recall record's shape), and the attempt's telemetry: the best score of a LIVE
    exchange that passes the gate, before the auto floor (None = nothing above the
    search floor), the groups rendered, the block's tokens and the milliseconds the
    attempt took."""

    block: str
    chats: list[dict]
    best_score: float | None
    groups: int
    tokens: int
    ms: int


# A word: a run of letters/digits, inner apostrophes and hyphens kept («что-то», "don't").
_WORD_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")


def gate_reason(text: str) -> str | None:
    """Why `text` is not searched, or None: fewer than GATE_MIN_WORDS once punctuation
    is dropped (see WHY in build_recall_block)."""
    words = len(_WORD_RE.findall(text or ""))
    return None if words >= GATE_MIN_WORDS else f"{words} words, fewer than {GATE_MIN_WORDS}"


async def build_recall_block(
    db, *, session_id: str, session: dict, user_id: str, project_id: str,
    persona_id: str, text: str, budget: int,
) -> RecallResult:
    """The persona's auto-recall block for this turn's `text` (see the module ARCH).

    Raises on a failure — the embedding call, a timed-out attempt (RECALL_TIMEOUT_S)
    or a query — so the caller turns it into a turn warning; the person must not read
    silence as "never discussed". The gate and a floor-empty result return an empty
    block instead (one log line each, nothing to warn about).
    """
    started = time.monotonic()
    reason = gate_reason(text)
    # WHY: a message under GATE_MIN_WORDS (punctuation dropped) is never embedded,
    # never scored — no embedding call, one log line. «привет», «спасибо», «ок, давай»
    # carry no subject, and the score floor alone does not keep a greeting from matching
    # old greetings; a word count is free, a model call is ruled out.
    if reason is not None:
        logger.info("chat auto-recall session=%s persona=%r skipped: %s — no search",
                    session_id, persona_id, reason)
        return RecallResult("", [], None, 0, 0, _ms(started))
    return await asyncio.wait_for(
        _recall(db, session_id=session_id, session=session, user_id=user_id,
                project_id=project_id, persona_id=persona_id, text=text, budget=budget,
                started=started),
        timeout=RECALL_TIMEOUT_S,
    )


async def _recall(
    db, *, session_id: str, session: dict, user_id: str, project_id: str,
    persona_id: str, text: str, budget: int, started: float,
) -> RecallResult:
    """One ungated attempt; every exit is exactly one `logger.info`."""
    # INVARIANT(security): the candidate set is exactly visible_sessions' output;
    # the queries below read only the session ids it returned.
    # Why: visible_sessions is the one wall between members and around deleted,
    # unindexed and hidden-subtree chats — a direct chat_tocs selection would
    # bypass it, and a synopsis restates the chat's text.
    visible, _ = await visible_for_chat(db, session_id, session, user_id, project_id)
    candidates = await _persona_candidates(db, visible, user_id, project_id, persona_id) if visible else []
    last_at = {c["sid"]: c["last_at"] for c in candidates if c["last_at"]}
    # `{sid: candidate}` — the KNN predicate and live_rows key on its ids; every
    # later read runs over them, never as a selection of its own.
    sessions = {c["sid"]: c for c in newest_branch_per_thread(candidates, last_at)}
    best_score: float | None = None
    block, shown, rendered, tokens = "", [], 0, 0
    floor = await settings.get("CHAT_RECALL_AUTO_MIN_SCORE")
    if sessions:
        chunks = await _score(db, user_id, project_id, sessions, text)
        hits = _collapse(chunks, await live_rows(db, {c["message_id"] for c in chunks}, sessions))
        # The best score is read off the LIVE, quotable exchanges: a chunk whose message
        # is gone since it was indexed is not a hit, and a small-talk exchange is no
        # subject — neither must read as the best score in the log.
        exchanges = _subjects(await _exchanges(db, sessions, hits))
        best_score = exchanges[0]["score"] if exchanges else None
        block, shown, rendered, tokens = render_recall(
            group_exchanges(sessions, [e for e in exchanges if e["score"] >= floor]), budget)
    ms = _ms(started)
    logger.info("chat auto-recall session=%s persona=%r candidates=%d best=%s floor=%.2f "
                "groups=%d tokens=%d ms=%d",
                session_id, persona_id, len(sessions),
                "-" if best_score is None else f"{best_score:.3f}", floor,
                rendered, tokens, ms)
    return RecallResult(block, shown, best_score, rendered, tokens, ms)


async def _score(db, user_id: str, project_id: str, sessions: dict, text: str) -> list[dict]:
    """The chunks of `sessions` nearest to `text`, best first — the union over the
    query pieces, a message keeping its best chunk's score. Nothing below the search
    floor (RETRIEVAL_MIN_SCORE, applied by _score_chunks) is ever returned."""
    pieces = [c.content for c in chunk_markdown(
        text, max_chunk_chars=await settings.get("RETRIEVAL_CHUNK_MAX_CHARS"),
        input_max_chars=await settings.get("EMBEDDING_INPUT_MAX_CHARS"))][:QUERY_MAX_PIECES]
    vectors = await embeddings.embed_texts(
        pieces, instruction=await settings.get("RETRIEVAL_QUERY_INSTRUCTION"))
    best: dict[str, dict] = {}
    for qvec in vectors:
        for chunk in await _score_chunks(db, user_id, project_id, sessions, qvec, Window()):
            held = best.get(chunk["message_id"])
            if held is None or chunk["score"] > held["score"]:
                best[chunk["message_id"]] = chunk
    return sorted(best.values(), key=lambda c: c["score"], reverse=True)


async def _exchanges(db, sessions: dict, hits: list[dict]) -> list[dict]:
    """The hits as whole exchanges `{user, answer, score}` — a user hit with its answer,
    an answer hit with its question — best first, one per user message. A side that
    is not a live message of a visible session is missing (the message-level re-check:
    live_rows re-states `deleted_at IS NONE AND chat_id IN $sids`); an answer whose
    question is gone is dropped — half an exchange quotes nobody."""
    parents = await live_rows(db, {h["message"]["parent_id"] for h in hits
                                   if h["message"]["role"] == "assistant" and h["message"].get("parent_id")},
                              sessions)
    asked = [h["message"]["id"] for h in hits if h["message"]["role"] == "user"]
    answers: dict[str, dict] = {}
    if asked:
        rows = await db.query(
            f"SELECT {MESSAGE_FIELDS} FROM messages WHERE parent_id IN $ids AND role = 'assistant' "
            "AND deleted_at IS NONE AND chat_id IN $sids ORDER BY created_at",
            {"ids": asked, "sids": sorted(sessions)},
        ) or []
        # Several replies under one message: the latest with text is the one that stands.
        for row in rows:
            if (row.get("content") or "").strip():
                answers[row["parent_id"]] = row
    out: list[dict] = []
    seen: set[str] = set()
    for hit in sorted(hits, key=lambda h: h["score"], reverse=True):
        message = hit["message"]
        if message["role"] == "user":
            user, answer = message, answers.get(message["id"])
        else:
            user = parents.get(message.get("parent_id") or "")
            answer = message
            if not user or user["role"] != "user":
                continue
        if user["id"] in seen:
            continue
        seen.add(user["id"])
        out.append({"user": user, "answer": answer, "score": hit["score"]})
    return out


def _subjects(exchanges: list[dict]) -> list[dict]:
    """The exchanges whose user words pass the gate — the query gate's other side.

    WHY: a «привет» → «Привет! Чем помочь?» exchange is the speech-act twin of any longer
    greeting (0.398 on gray once the corpus held one greeting chat — above five true
    matches of the calibration), and quoting it spends the row's one recall on nothing.
    Such an exchange stays findable by the model's own search; only the automatic recall
    skips it. Cost: a genuine question of under GATE_MIN_WORDS words in an old chat is
    not auto-recalled either.
    """
    return [e for e in exchanges if gate_reason(e["user"]["content"]) is None]


def group_exchanges(sessions: dict, exchanges: list[dict]) -> list[dict]:
    """Exchanges by topic — the chat's `chat_tocs` topic whose `message_ids` holds the
    exchange's user message — else by chat alone; best score first, at most
    MAX_GROUPS groups of MAX_EXCHANGES_PER_GROUP exchanges each. A group is
    `{sid, title, date, topic, exchanges}`: `date` is the day its best exchange was
    said; `topic` None for a chat group."""
    found: dict[tuple[str, int], dict] = {}
    for exchange in sorted(exchanges, key=lambda e: e["score"], reverse=True):
        chat = sessions[exchange["user"]["chat_id"]]
        topics = chat["topics"]
        index = next((i for i, t in enumerate(topics)
                       if exchange["user"]["id"] in (t.get("message_ids") or [])), -1)
        group = found.setdefault((chat["sid"], index), {
            "sid": chat["sid"], "title": chat["title"], "topic": topics[index] if index >= 0 else None,
            "date": _day(exchange["user"].get("created_at")), "exchanges": [],
        })
        if len(group["exchanges"]) < MAX_EXCHANGES_PER_GROUP:
            group["exchanges"].append(exchange)
    return sorted(found.values(), key=lambda g: g["exchanges"][0]["score"], reverse=True)[:MAX_GROUPS]


def render_recall(groups: list[dict], budget: int) -> tuple[str, list[dict], int, int]:
    """`(block, chats, groups rendered, tokens)` over `groups` (best first): the
    exchanges in order until one crosses `budget` (that one goes in too — the first
    always does), everything after it left out; ("", [], 0, 0) for an empty list. A
    group header is never emitted without its exchange; `chats` is `[{sid, date}]`,
    a chat once even when two of its topics are two groups."""
    if not groups:
        return "", [], 0, 0
    parts = [HEADER]
    used = count_tokens_approx(HEADER)
    shown: list[dict] = []
    rendered = 0
    for group in groups:
        for n, exchange in enumerate(group["exchanges"]):
            text = (f"{_group_line(group)}\n" if n == 0 else "") + _exchange_text(exchange)
            cost = count_tokens_approx("\n\n" + text)
            if shown and used + cost > budget:
                return "\n\n".join(parts), shown, rendered, used
            parts.append(text)
            used += cost
            if n == 0:
                rendered += 1
                if not any(c["sid"] == group["sid"] for c in shown):
                    shown.append({"sid": group["sid"], "date": group["date"]})
    return "\n\n".join(parts), shown, rendered, used


def _group_line(group: dict) -> str:
    """`date · chat title (chat <sid>) — topic: title — synopsis`; the `·` of a topic's
    own text is replaced (it is the model's words, and `·` separates fields here)."""
    line = f"{group['date']} · {group['title'] or 'Untitled'} (chat {group['sid']})"
    topic = group.get("topic")
    if topic:
        line += f" — topic: {topic['title']} — {topic['synopsis']}".replace("·", "-")
    return line


def _exchange_text(exchange: dict) -> str:
    user = exchange["user"]
    lines = [f"user (message {user['id']}) {_day(user.get('created_at'))}: "
             f"{_clip(user['content'], USER_CHARS)}"]
    answer = exchange.get("answer")
    if answer:
        lines.append(f"assistant (message {answer['id']}): {_clip(answer['content'], ANSWER_CHARS)}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _day(value: object) -> str:
    """The day a message was said, as an ISO date ("" when the row carries none)."""
    if isinstance(value, str):
        value = parse_iso(value) if value else None
    return value.date().isoformat() if isinstance(value, datetime) else ""


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
