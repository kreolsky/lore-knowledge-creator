"""Chat topics — label every chat of each memory-ON persona with a table of contents (chat_tocs).

# ARCH: a RECONCILIATION per (user, project), posted by the chat-recall trigger
# (chat_sessions.recall.schedule_chat_index) — never a writer hook. The pair
# task first seeds a branch's toc from its source's (seed_branch_tocs, no call),
# then drops every labelled chat's topics that cover an edited exchange
# (verify_tocs, prune_toc); then LABELS: every thread of the user whose origin
# persona's frontmatter turns chat memory ON, newest first (chats re-bound by
# the owner first), each by the thread's newest branch
# (newest_branch_per_thread; a compaction continuation is a thread of its own).
# Verification and due-ness read ONE light query per pass — message ids, order
# and content hashes, no text; full text is loaded only for a chat labelled
# now. A run that spends its whole call budget and saved a toc with no failed
# call posts its own continuation; any other run leaves the rest to the next
# trigger or the hourly sweep. A chat is labelled when its unlabelled tail
# reaches CHAT_TOPICS_CLOSE_EVERY exchanges, or holds any and has been idle
# CHAT_TOPICS_IDLE_MIN; the window is the next CHAT_TOPICS_WINDOW unfrozen
# exchanges, a full window freezes, a partial one only labels. The model is the
# one the chat ran on. The prompt and the exchange rendering are the bench's
# chunk-v2 exactly as measured (scripts/chat-topics-bench/bench.py) — editing
# either un-measures the labeller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import settings
from agent_config import parse_persona
from agent_config_load import instance_skill_disabled
from chat_sessions.conversation import (
    last_message_at,
    newest_branch_per_thread,
)
from chat_sessions.recall import (
    CHAT_RECALL_SKILL,
    CHAT_TOPICS_IDLE_MIN,
    mark_topics_first,
    schedule_chat_topics,
    schedule_chat_topics_more,
    take_rerun,
    take_topics_first,
)
from content_hash import hash_content
from fastapi import HTTPException
from model_access import require_model_access
from pipeline.extractor.utils import LLMHTTPError, call_llm_text
from token_usage import UsageActor, usage_actor

from db import get_db
from jobs.tasks.chat_topics_seed import save_toc, seed_branch_tocs

logger = logging.getLogger(__name__)

CHAT_TOPICS_CLOSE_EVERY = 12
CHAT_TOPICS_WINDOW = 60
CHAT_TOPICS_FREEZE_IDLE = timedelta(hours=24)
# Model calls per pair run; a cold backfill drains over the run's own
# continuations, one budget a minute, instead of one burst.
CHAT_TOPICS_RUN_CALLS = 6
# Passes per run when posts collapse onto it mid-run (the call budget above is
# shared across them); past it, the hourly sweep takes over.
CHAT_TOPICS_RERUNS = 3
# The bench's rendering limits and answer budget (bench.py run_chunk).
USER_CLIP, ASSISTANT_CLIP, ANSWER_MAX_TOKENS = 350, 150, 4000

_PROMPT = (Path(__file__).resolve().parents[2] / "configs" / "chat_topics_prompt.md"
           ).read_text(encoding="utf-8").rstrip("\n") + "\n\n"
_STITCH = ("Темы предыдущей части разговора: {titles}.\n"
           "Если обмены продолжают одну из этих тем — повтори её название дословно.\n\n")
_BYOK_PREFIX = "byok:"
# The light read: everything verification and due-ness need, no message text.
# WHY crypto::sha256 in the query: it equals hash_content (hashlib, UTF-8) of
# the same text, so the light read and the full read build one hash line.
_LIGHT_SQL = (
    "SELECT meta::id(id) AS id, chat_id, role, model, created_at, origin_id, "
    "crypto::sha256(content) AS h, string::trim(content) != '' AS has "
    "FROM messages WHERE chat_id IN $ids AND deleted_at IS NONE"
)

# WHY one call at a time per worker process: the labeller usually is the local
# AI host, which sits near its memory ceiling; a sweep posting every pair must
# queue here, not burst there. Restart-frozen like the embeddings bound.
_llm_slot = asyncio.Semaphore(1)


def clip(text: str, limit: int) -> str:
    """The bench's clip: whitespace collapsed, cut at `limit` with an ellipsis."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


# ─── who and what is labelled ────────────────────────────────────────────────


async def memory_personas(db, project_id: str) -> set[str]:
    """Ids of the project's personas whose frontmatter turns chat memory ON."""
    folders = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND is_system = true AND system_role = 'system_prompt' AND deleted_at IS NONE",
        {"pid": project_id},
    ) or []
    if not folders:
        return set()
    personas = await db.query(
        "SELECT meta::id(id) AS id, content FROM documents WHERE project_id = $pid "
        "AND parent_id IN $ids AND deleted_at IS NONE",
        {"pid": project_id, "ids": [f["id"] for f in folders]},
    ) or []
    return {p["id"] for p in personas
            if parse_persona(p.get("content") or "").chat_memory_budget > 0}


async def mark_persona_memory(db, project_id: str, sessions: list[dict]) -> list[dict]:
    """Set each serialized session's `persona_memory` — whether its persona has
    conversation memory ON (the list row's brain mark).
    WHY: every response that hands the client a session row carries it — the
    client replaces the row wholesale (create inserts it, PATCH swaps it), so a
    row without the key loses the mark until the next list reload."""
    remembering = await memory_personas(db, project_id)
    for s in sessions:
        s["persona_memory"] = s.get("system_prompt_id") in remembering
    return sessions


async def labelled_chats(db, user_id: str, project_id: str, personas: set[str]) -> list[dict]:
    """`[{sid, persona_id, forked_from, last_at, model, exchanges}]` newest first: the
    newest branch (newest_branch_per_thread) of every thread of the user with
    a memory-ON origin persona that holds messages and is not `unindexed`.
    `exchanges` are the light ones (group_exchanges over `_LIGHT_SQL` rows),
    `model` the chat's latest, `forked_from` the branch's source session."""
    if not personas:
        return []
    rows = await db.query(
        "SELECT meta::id(id) AS sid, thread_id, origin_persona_id, created_at, "
        "unindexed, forked_from_session FROM chat_sessions WHERE user_id = $uid AND project_id = $pid "
        "AND deleted_at IS NONE AND is_note = false",
        {"uid": user_id, "pid": project_id},
    ) or []
    # WHY both filters before the pick: they leave the pick's input equal to
    # visible_sessions' (the block's), and the persona and unindexed are
    # per-conversation, so they never split a thread.
    rows = [{**r, "root": r.get("thread_id") or r["sid"]} for r in rows
            if not r.get("unindexed") and r.get("origin_persona_id") in personas]
    last_at = await last_message_at(db, [r["sid"] for r in rows])
    shown = newest_branch_per_thread(rows, last_at)
    if not shown:
        return []
    light = await db.query(_LIGHT_SQL, {"ids": [r["sid"] for r in shown]}) or []
    by_chat: dict[str, list[dict]] = {}
    for m in light:
        by_chat.setdefault(m["chat_id"], []).append(m)
    chats = []
    for r in shown:
        if r["sid"] not in by_chat:
            continue
        exchanges, model = group_exchanges(by_chat[r["sid"]])
        chats.append({"sid": r["sid"], "persona_id": r["origin_persona_id"],
                      "forked_from": r.get("forked_from_session"),
                      "last_at": last_at[r["sid"]], "model": model, "exchanges": exchanges})
    return sorted(chats, key=lambda c: c["last_at"], reverse=True)


# ─── exchanges and the model answer ─────────────────────────────────────────


def group_exchanges(rows: list[dict]) -> tuple[list[dict], str]:
    """`(exchanges, model)` over one chat's message rows.

    Each exchange is `{id, user, answers, hash}`: id = the user message's,
    `user` its row, `answers` the rows after it, `hash` = exchange_hash;
    `model` is the latest assistant row's. A trailing exchange with
    no answer text (`has`) is the turn in flight and is left out. Rows carry
    `id, role, created_at, h, has` (+ `model`, `content`). The light read and
    the full read both group HERE — a divergence would flap every toc.
    """
    rows = sorted(rows, key=lambda m: (str(m.get("created_at") or ""), m.get("role") != "user"))
    exchanges: list[dict] = []
    model = ""
    for m in rows:
        if m.get("role") == "user":
            exchanges.append({"id": m["id"], "user": m, "answers": []})
        elif exchanges:
            exchanges[-1]["answers"].append(m)
            model = m.get("model") or model
    if exchanges and not any(a.get("has") for a in exchanges[-1]["answers"]):
        exchanges.pop()
    for ex in exchanges:
        ex["hash"] = exchange_hash(ex)
    return exchanges, model


def exchange_hash(ex: dict) -> str:
    """hash_content of `user h \x1f answer h…` — the exchange's content only.

    WHY no id: the id is bound by the alignment of labelled_hashes to
    labelled_ids, and a content-only hash is what lets a branch's copy (new
    ids, same text) be compared with its source (seed_toc)."""
    return hash_content("\x1f".join([ex["user"]["h"], *(a["h"] for a in ex["answers"])]))


async def chat_exchanges(db, chat_id: str) -> list[dict]:
    """The chat's exchanges in order with their text — `{id, hash, user, asst}`,
    texts rendered as the bench clips them (group_exchanges' grouping)."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, role, content, created_at FROM messages "
        "WHERE chat_id = $cid AND deleted_at IS NONE",
        {"cid": chat_id},
    ) or []
    for m in rows:
        text = m.get("content") or ""
        m["h"], m["has"] = hash_content(text), bool(text.strip())
    exchanges, _ = group_exchanges(rows)
    return [{"id": ex["id"], "hash": ex["hash"],
             "user": clip(ex["user"].get("content") or "", USER_CLIP),
             "asst": clip("\n".join(a["content"] for a in ex["answers"] if a.get("content")),
                          ASSISTANT_CLIP)}
            for ex in exchanges]


def parse_topics(text: str, ids: list[str]) -> list[dict]:
    """The answer's topics over the window `ids` as `{title, synopsis, message_ids}`.

    The bench's parse: <think> stripped, the first `[...]` read as JSON; turns
    outside the window dropped, an equal title merged, an exchange no topic
    covers inherits the previous exchange's topics (the first: the next
    covered one's). No usable topic ⇒ [] (the caller logs a failure)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except json.JSONDecodeError:
        items = []
    topics: dict[str, dict] = {}
    owners: list[list[str]] = [[] for _ in ids]
    for item in items if isinstance(items, list) else []:
        title = item.get("title") if isinstance(item, dict) else None
        if not isinstance(title, str) or not title.strip():
            continue
        title = title.strip()
        topics.setdefault(title, {"title": title, "synopsis": str(item.get("synopsis") or "").strip()})
        for n in item.get("turns") or []:
            if isinstance(n, int) and not isinstance(n, bool) and 0 <= n < len(ids) \
                    and title not in owners[n]:
                owners[n].append(title)
    covered = [k for k, o in enumerate(owners) if o]
    if not covered:
        return []
    for k in range(len(owners)):
        if not owners[k]:
            owners[k] = owners[k - 1] if k else owners[covered[0]]
    for t in topics.values():
        t["message_ids"] = [ids[k] for k, o in enumerate(owners) if t["title"] in o]
    return [t for t in topics.values() if t["message_ids"]]


# ─── one chat ────────────────────────────────────────────────────────────────


def merge_window(toc: dict, order: list[str], window: list[str],
                 found: list[dict], freeze: bool) -> dict:
    """The toc after labelling `window`: earlier topics keep only their frozen
    exchanges, a found topic whose title equals an earlier one joins it, and a
    freezing window adds its exchanges to frozen_ids. `order` = every exchange id."""
    frozen = set(toc["frozen_ids"]) | (set(window) if freeze else set())
    kept = [{**t, "message_ids": [m for m in t["message_ids"] if m in toc["frozen_ids"]]}
            for t in toc["topics"]]
    by_title = {t["title"]: t for t in kept if t["message_ids"]}
    for t in found:
        same = by_title.get(t["title"])
        if same:
            same["message_ids"] = list(dict.fromkeys(same["message_ids"] + t["message_ids"]))
        else:
            by_title[t["title"]] = dict(t)
    labelled = frozen | set(window)
    pos = {m: k for k, m in enumerate(order)}
    topics = sorted(by_title.values(), key=lambda t: min(pos.get(m, 0) for m in t["message_ids"]))
    for t in topics:
        t["message_ids"].sort(key=lambda m: pos.get(m, 0))
        t["frozen"] = all(m in frozen for m in t["message_ids"])
    return {**toc, "topics": topics,
            "frozen_ids": [m for m in order if m in frozen],
            "labelled_ids": [m for m in order if m in labelled]}


async def _ask(model: str, window: list[dict], earlier: list[str]) -> list[dict]:
    """One labelling call over `window`; raises when the answer holds no topic."""
    body = "\n".join(f"{n}. Пользователь: {e['user']}\n   Ассистент: {e['asst']}"
                     for n, e in enumerate(window))
    stitch = _STITCH.format(titles=", ".join(f"«{t}»" for t in earlier)) if earlier else ""
    async with _llm_slot:
        text = await call_llm_text(_PROMPT + stitch + body, model=model,
                                   max_tokens=ANSWER_MAX_TOKENS)
    found = parse_topics(text, [e["id"] for e in window])
    if not found:
        raise ValueError(f"no parseable topic array in the answer: {text[:300]!r}")
    return found


def prune_toc(toc: dict, exchanges: list[dict]) -> dict | None:
    """The toc without every topic that covers a changed exchange; `toc`
    itself when nothing changed, None when no topic is left.

    Changed = a labelled id whose current exchange_hash differs from its
    stored one (labelled_hashes, aligned to labelled_ids) or that vanished. The
    kept topics' exchanges stay labelled (+ frozen), the rest become tail and
    are labelled again by the normal pass. `exchanges` carry `{id, hash}`."""
    labelled = toc.get("labelled_ids") or []
    stored = dict(zip(labelled, toc.get("labelled_hashes") or []))
    current = {e["id"]: e["hash"] for e in exchanges}
    changed = {m for m in labelled if m not in current or current[m] != stored.get(m)}
    if not changed:
        return toc
    topics = [t for t in toc.get("topics") or [] if not changed & set(t["message_ids"])]
    if not topics:
        return None
    kept = {m for t in topics for m in t["message_ids"]}
    ids = [m for m in labelled if m in kept]
    return {**toc, "topics": topics, "labelled_ids": ids,
            "labelled_hashes": [stored[m] for m in ids],
            "frozen_ids": [m for m in toc.get("frozen_ids") or [] if m in kept]}


async def _store_pruned(db, chat_id: str, pruned: dict | None) -> None:
    """Write prune_toc's verdict for a changed toc: delete the row or narrow it."""
    if pruned is None:
        await db.query("DELETE type::record('chat_tocs', $cid)", {"cid": chat_id})
        logger.info("chat topics: toc of chat %s dropped — every topic covered a changed exchange",
                    chat_id)
        return
    await db.query(
        "UPDATE type::record('chat_tocs', $cid) SET topics = $t, labelled_ids = $l, "
        "labelled_hashes = $h, frozen_ids = $f",
        {"cid": chat_id, "t": pruned["topics"], "l": pruned["labelled_ids"],
         "h": pruned["labelled_hashes"], "f": pruned["frozen_ids"]},
    )
    logger.info("chat topics: toc of chat %s pruned to %d topics — a labelled exchange changed",
                chat_id, len(pruned["topics"]))


async def verify_tocs(db, user_id: str, project_id: str,
                      exchanges: dict[str, list[dict]]) -> tuple[int, dict[str, dict]]:
    """Prune the labelled chats' chat_tocs rows whose labelled exchanges
    changed (prune_toc); returns (how many rows pruned or dropped, the
    surviving rows by chat id). `exchanges` = the light exchanges of exactly
    the labelled chats, by chat id. No model call."""
    if not exchanges:
        return 0, {}
    # INVARIANT(persisted): a chat's topics live as long as the chat row — no toc is deleted
    # for its chat being deleted, unindexed or re-bound, its persona deleted
    # or switched off, chat-recall disabled, or its branch no longer the newest. Why: operator
    # ruling — a topic is kept while its chat is kept; who may read it is decided at read time
    # (visible_sessions, the session guard, the shared branch pick), and a persona switched
    # back on continues the thread at once, without re-labelling.
    rows = await db.query(
        "SELECT chat_id, labelled_ids, labelled_hashes, frozen_ids, topics, model FROM chat_tocs "
        "WHERE user_id = $uid AND project_id = $pid AND chat_id IN $ids",
        {"uid": user_id, "pid": project_id, "ids": sorted(exchanges)},
    ) or []
    # INVARIANT(security): every topic covering an edited or vanished exchange is dropped on
    # EVERY pair run, before and regardless of the off-switch and call budget; others are kept.
    # Why: text the user edited away must not survive in a synopsis — the same intent as
    # chat_index's purge of what the user deleted; messages are edited in place, so only the
    # hash can tell.
    kept: dict[str, dict] = {}
    touched = 0
    for toc in rows:
        pruned = prune_toc(toc, exchanges[toc["chat_id"]])
        if pruned is not toc:
            touched += 1
            await _store_pruned(db, toc["chat_id"], pruned)
        if pruned is not None:
            kept[toc["chat_id"]] = pruned
    return touched, kept


def label_due(chat: dict, toc: dict | None, now: datetime) -> str | None:
    """What a pass owes the chat, from its light exchanges and verified toc:
    "call" (the tail is due), "freeze" (a day idle, tail labelled, unfrozen
    exchanges left — no call), or None."""
    order = [e["id"] for e in chat["exchanges"]]
    labelled = set((toc or {}).get("labelled_ids") or [])
    tail = [m for m in order if m not in labelled]
    idle = now - chat["last_at"]
    if tail:
        due = len(tail) >= CHAT_TOPICS_CLOSE_EVERY or idle >= timedelta(minutes=CHAT_TOPICS_IDLE_MIN)
        return "call" if due else None
    if toc and toc.get("model") is not None and idle >= CHAT_TOPICS_FREEZE_IDLE \
            and len(order) > len(toc.get("frozen_ids") or []):
        return "freeze"
    return None


async def _load_toc(db, chat: dict, exchanges: list[dict]) -> dict:
    """The chat's stored toc pruned to the chat's current exchanges
    (prune_toc, the change written), or a fresh one when none is left."""
    fresh = {"frozen_ids": [], "labelled_ids": [], "topics": [], "labelled_hashes": [], "model": None}
    rows = await db.query(
        "SELECT frozen_ids, labelled_ids, topics, labelled_hashes, model "
        "FROM type::record('chat_tocs', $cid)", {"cid": chat["sid"]},
    ) or []
    if not rows:
        return fresh
    toc = rows[0]
    # WHY re-checked here though verify_tocs ran: an edit may land between the
    # pair run's verification pass and this chat's turn in the label loop
    # (see the INVARIANT in verify_tocs).
    pruned = prune_toc(toc, exchanges)
    if pruned is not toc:
        await _store_pruned(db, chat["sid"], pruned)
    return {**fresh, **pruned} if pruned is not None else fresh


async def _labeller_model(db, chat: dict, user_id: str, model: str) -> str | None:
    """The model the chat ran on; CHAT_MODEL when unrecorded; None for a
    user's own connection (byok), which the worker cannot call yet, and for a
    model the user may no longer run."""
    if model.startswith(_BYOK_PREFIX):
        logger.info("chat topics: chat %s ran on %s (byok) — not labelled", chat["sid"], model)
        return None
    if model:
        return model if await _user_may_run(db, chat, user_id, model) else None
    fallback = await settings.get("CHAT_MODEL")
    logger.info("chat topics: chat %s has no recorded model — labelling with CHAT_MODEL %s",
                chat["sid"], fallback)
    return fallback


async def _user_may_run(db, chat: dict, user_id: str, model: str) -> bool:
    """The turn gate's verdict (`require_model_access`) for the chat's owner.

    WHY no CHAT_MODEL fallback on a refusal: the labeller would then spend the
    instance key on a chat whose user lost the model — the chat stays unlabelled
    until access returns, like a byok chat.
    """
    rows = await db.query("SELECT role FROM type::record('users', $id)", {"id": user_id}) or []
    try:
        await require_model_access({"user_id": user_id, "role": (rows[0] if rows else {}).get("role")}, model)
    except HTTPException:
        logger.info("chat topics: chat %s ran on %s, no longer allowed for its user — not labelled",
                    chat["sid"], model)
        return False
    return True


def _router_refused(exc: Exception) -> bool:
    """True when the router refused the model itself — permanent for this run.

    WHY the body and not the status: a wrong AI_API_KEY also answers 403 and
    must keep failing loudly; only the router's own codes name the model —
    `model_not_allowed` (outside the key's list) and `model_not_found`
    (unknown to an unrestricted key).
    """
    return isinstance(exc, LLMHTTPError) and any(
        code in exc.body for code in ("model_not_allowed", "model_not_found"))


async def label_chat(db, chat: dict, user_id: str, project_id: str, calls_left: int,
                     model: str | None) -> tuple[int, int, bool, str | None]:
    """Label one due chat (label_due) as far as its tail is due with `model`
    (None = no call, the freeze only); returns (model calls, tocs saved, a call
    failed, the model if the router refused it). The only reader of the chat's
    full text."""
    exchanges = await chat_exchanges(db, chat["sid"])
    toc = await _load_toc(db, chat, exchanges)
    idle = datetime.now(timezone.utc) - chat["last_at"]
    order = [e["id"] for e in exchanges]
    calls = saved = 0
    while calls < calls_left:
        tail = [m for m in order if m not in toc["labelled_ids"]]
        if not tail or (len(tail) < CHAT_TOPICS_CLOSE_EVERY and idle < timedelta(minutes=CHAT_TOPICS_IDLE_MIN)):
            break
        if model is None:
            return calls, saved, False, None
        window = [e for e in exchanges if e["id"] not in toc["frozen_ids"]][:CHAT_TOPICS_WINDOW]
        earlier = [t["title"] for t in toc["topics"] if any(m in toc["frozen_ids"] for m in t["message_ids"])]
        calls += 1
        try:
            found = await _ask(model, window, earlier)
        except Exception as exc:
            if _router_refused(exc):
                logger.info("chat topics: chat %s ran on %s, refused by the router — not labelled",
                            chat["sid"], model)
                return calls, saved, False, model
            logger.exception("chat topics: labelling failed chat=%s model=%s", chat["sid"], model)
            return calls, saved, True, None
        ids = [e["id"] for e in window]
        last_window = len(window) == len(order) - len(toc["frozen_ids"])
        freeze = len(window) == CHAT_TOPICS_WINDOW or (last_window and idle >= CHAT_TOPICS_FREEZE_IDLE)
        toc = {**merge_window(toc, order, ids, found, freeze), "model": model}
        await save_toc(db, chat, user_id, project_id, model, toc, exchanges)
        saved += 1
    if idle >= CHAT_TOPICS_FREEZE_IDLE and toc.get("model") is not None \
            and len(toc["labelled_ids"]) == len(order) > len(toc["frozen_ids"]):
        # A day idle with the tail already labelled: freeze it, no call owed.
        toc = {**toc, "frozen_ids": list(toc["labelled_ids"]),
               "topics": [{**t, "frozen": True} for t in toc["topics"]]}
        await save_toc(db, chat, user_id, project_id, toc["model"], toc, exchanges)
    return calls, saved, False, None


# ─── tasks ───────────────────────────────────────────────────────────────────


async def _pair_pass(db, user_id: str, project_id: str, calls_left: int,
                     first: set[str], refused: set[str]) -> tuple[int, int, int, bool, set[str]]:
    """One seed + verify + label pass, the chats in `first` taken first, chats
    on a model in `refused` skipped; returns (tocs pruned, model calls, tocs
    saved, a call failed, the refused models so far)."""
    chats = await labelled_chats(db, user_id, project_id, await memory_personas(db, project_id))
    await seed_branch_tocs(db, user_id, project_id, chats)
    dropped, tocs = await verify_tocs(db, user_id, project_id,
                                      {c["sid"]: c["exchanges"] for c in chats})
    calls = saved = 0
    failed = False
    if chats and not await instance_skill_disabled(db, CHAT_RECALL_SKILL):
        now = datetime.now(timezone.utc)
        for chat in sorted(chats, key=lambda c: c["sid"] not in first):  # stable: newest first within
            if calls >= calls_left:
                break
            owed = label_due(chat, tocs.get(chat["sid"]), now)
            model = await _labeller_model(db, chat, user_id, chat["model"]) if owed == "call" else None
            if owed is None or (owed == "call" and model is None):
                continue
            if model in refused:
                continue
            c, s, f, r = await label_chat(db, chat, user_id, project_id, calls_left - calls, model)
            calls, saved, failed = calls + c, saved + s, failed or f
            if r:
                refused = refused | {r}
    return dropped, calls, saved, failed, refused


async def chat_topics_pair_task(ctx, user_id: str, project_id: str) -> None:
    """Reconcile one (user, project) pair's chat_tocs — see the module ARCH."""
    usage_actor.set(UsageActor(source="chat_topics", user_id=user_id, project_id=project_id))
    db = await get_db()
    await take_rerun(ctx)
    first: set[str] = set()
    refused: set[str] = set()
    dropped = calls = saved = 0
    failed = False
    for _ in range(CHAT_TOPICS_RERUNS):
        first |= await take_topics_first(ctx, user_id, project_id)
        d, c, s, f, refused = await _pair_pass(db, user_id, project_id,
                                               CHAT_TOPICS_RUN_CALLS - calls, first, refused)
        dropped, calls, saved, failed = dropped + d, calls + c, saved + s, failed or f
        # WHY: a post that lands after this check and before arq finishes the
        # job sets a flag no pass consumes (the next run clears it at start) —
        # milliseconds wide; the hourly sweep reconciles it (see the INVARIANT
        # in recall._post_reconcile).
        if not await take_rerun(ctx):
            break
    # WHY all three: a spent budget means work may remain; a saved toc and no
    # failed call mean the labeller is up — a down one must not loop every
    # minute, the hourly sweep retries it.
    more = calls >= CHAT_TOPICS_RUN_CALLS and saved > 0 and not failed
    if more:
        # WHY re-mark: the priority ids were popped at this run's start; a budget
        # spent before they were all labelled must not demote the rest to
        # newest-first. Already-labelled ones are not due and cost nothing.
        await mark_topics_first(user_id, project_id, first)
        await schedule_chat_topics_more(user_id, project_id)
    if dropped or calls:
        logger.info("chat topics user=%s project=%s: pruned %d stale tocs, %d model calls, "
                    "%d saved, failed=%s, continuation=%s",
                    user_id, project_id, dropped, calls, saved, failed, more)


async def chat_topics_idle_task(ctx, user_id: str, project_id: str) -> None:
    """The deferred turn-end check: the chat may have gone idle — reconcile."""
    await schedule_chat_topics(user_id, project_id)


async def chat_topics_sweep_task(ctx) -> None:
    """Hourly catch-up root: post the pair task for every pair with an AI chat
    in a project that has a memory-ON persona."""
    db = await get_db()
    pairs: set[tuple[str, str]] = set()
    chat_pairs = await db.query(
        "SELECT user_id, project_id FROM chat_sessions "
        "WHERE deleted_at IS NONE AND is_note = false GROUP BY user_id, project_id",
    ) or []
    memory_on: dict[str, bool] = {}
    for r in chat_pairs:
        pid = r["project_id"]
        if pid not in memory_on:
            memory_on[pid] = bool(await memory_personas(db, pid))
        if memory_on[pid]:
            pairs.add((r["user_id"], pid))
    for user_id, project_id in sorted(pairs):
        await schedule_chat_topics(user_id, project_id)
    if pairs:
        logger.info("chat topics sweep: posted %d pairs", len(pairs))
