"""chat-recall index — reconcile a (user, project)'s chat_chunks against its live messages.

# ARCH: the index is a RECONCILIATION, never a writer hook. The pair task diffs
# the pair's live, indexable messages against chat_chunks by (content_hash,
# model): it purges chunks whose message is gone / deleted, whose session is
# deleted / a note / `unindexed`, or whose hash or model is stale; then embeds
# the pending messages, newest first, up to CHAT_INDEX_RUN_BUDGET chunks.
# A run RE-PLANS until a pass finds no work or the budget is spent: a trigger
# posted while the run is in flight collapses onto it (same job_id), so the run
# itself must pick up what landed meanwhile. Leftovers past the budget wait for
# the next trigger or the hourly sweep — the task never re-enqueues itself (a
# self-post under the same job_id collapses onto the running job, jobs/pool.py).
# While the skill is disabled or embeddings are unconfigured nothing is
# embedded, but what is no longer indexable is still purged. Writers only post
# the trigger (chat_sessions.recall.schedule_chat_index).
"""
from __future__ import annotations

import logging
from collections import defaultdict

import settings
from chat_sessions.conversation import origin_of
from chat_sessions.recall import chat_recall_disabled, schedule_chat_index
from content_hash import hash_content
from markdown_chunker import chunk_markdown

import embeddings
from db import get_db

logger = logging.getLogger(__name__)

# Chunks written per pair run (embedded or reused). A cold corpus drains over
# the next triggers and hourly sweeps instead of one provider burst.
CHAT_INDEX_RUN_BUDGET = 200
# Plan passes per run; only bounds a writer that keeps landing messages faster
# than the run drains them.
CHAT_INDEX_MAX_PASSES = 5


async def _live_messages(db, user_id: str, project_id: str) -> list[dict]:
    """The pair's indexable messages: live, non-empty, in a live non-note,
    non-`unindexed` session the user owns in this project.

    WHY no documents.chats_unindexed filter: hidden-document chats stay
    indexed — inside-subtree viewers and digest both search them through the
    same index; filtering here would silently kill both (the hide is a
    read-time predicate in recall_search.visible_sessions).
    """
    # DEBT: every search (index_coverage) and every turn-end reconcile reads the
    # pair's whole message corpus plus all its chunks — O(corpus) per call; a
    # stored per-message hash would let the diff run in the DB. Why deferred: a
    # small trusted group's corpora are small, and the full diff is what keeps
    # the index a disposable, self-healing cache.
    sessions = await db.query(
        "SELECT meta::id(id) AS sid FROM chat_sessions "
        "WHERE user_id = $uid AND project_id = $pid AND deleted_at IS NONE "
        "AND is_note = false AND unindexed = false",
        {"uid": user_id, "pid": project_id},
    ) or []
    if not sessions:
        return []
    rows = await db.query(
        "SELECT meta::id(id) AS id, chat_id, role, content, created_at, origin_id "
        "FROM messages WHERE chat_id IN $sids AND deleted_at IS NONE",
        {"sids": [s["sid"] for s in sessions]},
    ) or []
    return [r for r in rows if (r.get("content") or "").strip()]


async def plan_chat_index(
    db, user_id: str, project_id: str, model: str,
) -> tuple[list[dict], list[str]]:
    """`(pending messages newest first, stale chunk ids)` for the pair.

    A message is indexed iff it has chunks and every one carries its current
    content hash and the current model; any stale chunk re-queues the WHOLE
    message (all its chunks are dropped), so a message is never half-indexed.
    """
    messages = await _live_messages(db, user_id, project_id)
    fresh, stale = await _fresh_and_stale(db, messages, user_id, project_id, model)
    pending = [m for m in messages if m["id"] not in fresh]
    pending.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    return pending, stale


async def index_coverage(
    db, user_id: str, project_id: str, model: str, exclude_chat_ids: set[str],
    only_chat_ids: set[str] | None = None,
) -> tuple[int, int]:
    """`(indexed, total)` over the pair's indexable messages outside
    `exclude_chat_ids` (and inside `only_chat_ids` when given — the sessions a
    scoped search actually searched) — the same predicate and freshness rule as
    the plan, so a reader's `indexed: N of M` never disagrees with what the task does."""
    messages = [m for m in await _live_messages(db, user_id, project_id)
                if m["chat_id"] not in exclude_chat_ids
                and (only_chat_ids is None or m["chat_id"] in only_chat_ids)]
    fresh, _ = await _fresh_and_stale(db, messages, user_id, project_id, model)
    return sum(1 for m in messages if m["id"] in fresh), len(messages)


async def _fresh_and_stale(
    db, messages: list[dict], user_id: str, project_id: str, model: str,
) -> tuple[set[str], list[str]]:
    """`(ids of fully indexed messages, ids of stale chunks)` for the pair."""
    want = {m["id"]: hash_content(m["content"]) for m in messages}
    chunks = await db.query(
        "SELECT meta::id(id) AS id, message_id, content_hash, model FROM chat_chunks "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": user_id, "pid": project_id},
    ) or []
    by_msg: dict[str, list[dict]] = defaultdict(list)
    for c in chunks:
        by_msg[c["message_id"]].append(c)
    fresh = {
        mid for mid, cs in by_msg.items()
        if all(c["content_hash"] == want.get(mid) and c["model"] == model for c in cs)
    }
    stale = [c["id"] for mid, cs in by_msg.items() if mid not in fresh for c in cs]
    return fresh, stale


async def _stored_vectors(db, hashes: list[str], model: str) -> dict[tuple[str, str], list]:
    """Vectors already stored under (content_hash, model), keyed by
    (content_hash, chunk text) — a branch copy of a message costs no embed."""
    if not hashes:
        return {}
    rows = await db.query(
        "SELECT content_hash, content, embedding FROM chat_chunks "
        "WHERE content_hash IN $hs AND model = $m",
        {"hs": hashes, "m": model},
    ) or []
    return {(r["content_hash"], r["content"]): r["embedding"] for r in rows}


async def _chunk_rows(
    message: dict, texts: list[str], user_id: str, project_id: str, model: str,
    vectors: dict[tuple[str, str], list],
) -> list[dict]:
    """The message's chat_chunks rows; only chunk texts with no stored vector
    reach the embedding API, and their vectors join `vectors` for the run."""
    h = hash_content(message["content"])
    missing = [t for t in dict.fromkeys(texts) if (h, t) not in vectors]
    if missing:
        # Chunks are a document: embedded PLAIN (see the INVARIANT of embed_texts).
        for t, vec in zip(missing, await embeddings.embed_texts(missing), strict=True):
            vectors[(h, t)] = vec
    return [{
        "user_id": user_id, "project_id": project_id,
        "chat_id": message["chat_id"], "message_id": message["id"],
        "origin_key": origin_of(message), "role": message["role"],
        "created_at": message["created_at"], "ord": i, "content": t,
        "content_hash": h, "model": model, "embedding": vectors[(h, t)],
    } for i, t in enumerate(texts)]


async def _index_pending(
    db, pending: list[dict], user_id: str, project_id: str, model: str, budget: int,
) -> int:
    """Embed + write pending messages up to `budget` chunks; returns chunks written.
    One INSERT per message, so a crash never leaves a message half-indexed."""
    max_chars = await settings.get("RETRIEVAL_CHUNK_MAX_CHARS")
    input_max = await settings.get("EMBEDDING_INPUT_MAX_CHARS")
    vectors = await _stored_vectors(
        db, sorted({hash_content(m["content"]) for m in pending}), model)
    written = 0
    for message in pending:
        texts = [c.content for c in chunk_markdown(
            message["content"], max_chunk_chars=max_chars, input_max_chars=input_max)]
        if not texts:
            continue
        if written and written + len(texts) > budget:
            break
        rows = await _chunk_rows(message, texts, user_id, project_id, model, vectors)
        await db.query("INSERT INTO chat_chunks $rows", {"rows": rows})
        written += len(rows)
    return written


async def _embedding_model() -> str | None:
    """The current EMBEDDING_MODEL, or None when embeddings are unconfigured."""
    try:
        await embeddings._ensure_config()
    except embeddings.EmbeddingConfigError:
        return None
    return await settings.get("EMBEDDING_MODEL")


async def _purge(db, user_id: str, project_id: str, chunk_ids: list[str]) -> None:
    if chunk_ids:
        await db.query(
            "DELETE chat_chunks WHERE user_id = $uid AND project_id = $pid "
            "AND meta::id(id) IN $ids",
            {"uid": user_id, "pid": project_id, "ids": chunk_ids},
        )


async def _purge_unindexable(db, user_id: str, project_id: str) -> int:
    """Delete the pair's chunks whose message is no longer indexable; returns
    how many. Needs no model, so it runs while indexing itself is off."""
    # INVARIANT(security): what the user deleted or marked unindexed leaves
    # chat_chunks even while the skill is disabled or embeddings are unconfigured.
    # Why: the read-time predicate keeps it from being served, but a deletion
    # must not leave the text stored for as long as the admin keeps recall off.
    chunks = await db.query(
        "SELECT meta::id(id) AS id, message_id FROM chat_chunks "
        "WHERE user_id = $uid AND project_id = $pid",
        {"uid": user_id, "pid": project_id},
    ) or []
    if not chunks:
        return 0
    live = {m["id"] for m in await _live_messages(db, user_id, project_id)}
    gone = [c["id"] for c in chunks if c["message_id"] not in live]
    await _purge(db, user_id, project_id, gone)
    return len(gone)


async def indexing_state(db) -> tuple[str, str | None]:
    """`(state, model)`: `("on", model)`, or `("skill_disabled" |
    "embeddings_unconfigured", None)` while indexing is off."""
    if await chat_recall_disabled(db):
        return "skill_disabled", None
    model = await _embedding_model()
    if model is None:
        return "embeddings_unconfigured", None
    return "on", model


async def _indexing_model(db) -> str | None:
    """The model to index under, or None while indexing is off (skill disabled
    or embeddings unconfigured)."""
    return (await indexing_state(db))[1]


async def index_chat_messages(ctx, user_id: str, project_id: str) -> None:
    """Reconcile one (user, project) pair's chat_chunks — see the module ARCH."""
    db = await get_db()
    model = await _indexing_model(db)
    if model is None:
        purged = await _purge_unindexable(db, user_id, project_id)
        if purged:
            logger.info("chat index off user=%s project=%s: purged %d chunks",
                        user_id, project_id, purged)
        return
    purged = written = 0
    for _ in range(CHAT_INDEX_MAX_PASSES):
        pending, stale = await plan_chat_index(db, user_id, project_id, model)
        await _purge(db, user_id, project_id, stale)
        wrote = await _index_pending(
            db, pending, user_id, project_id, model, CHAT_INDEX_RUN_BUDGET - written)
        purged, written = purged + len(stale), written + wrote
        if (not stale and not wrote) or written >= CHAT_INDEX_RUN_BUDGET:
            break
    if purged or written:
        logger.info("chat index user=%s project=%s: purged %d, wrote %d chunks",
                    user_id, project_id, purged, written)


async def _index_pairs(db) -> set[tuple[str, str]]:
    """Every (user, project) that has an AI chat or still holds chunks."""
    rows = (await db.query(
        "SELECT user_id, project_id FROM chat_sessions "
        "WHERE deleted_at IS NONE AND is_note = false GROUP BY user_id, project_id",
    ) or []) + (await db.query(
        "SELECT user_id, project_id FROM chat_chunks GROUP BY user_id, project_id",
    ) or [])
    return {(r["user_id"], r["project_id"]) for r in rows}


async def pairs_with_work(db, model: str) -> list[tuple[str, str, int, int]]:
    """`(user_id, project_id, pending messages, stale chunks)` for every pair
    whose plan has work — the population the sweep posts and the admin stats
    count, so a number shown is always one a pair task will drain."""
    out = []
    for user_id, project_id in sorted(await _index_pairs(db)):
        pending, stale = await plan_chat_index(db, user_id, project_id, model)
        if pending or stale:
            out.append((user_id, project_id, len(pending), len(stale)))
    return out


async def chat_index_sweep_task(ctx) -> None:
    """Hourly catch-up root: post the pair task for every pair whose plan has
    work (pending messages or stale chunks). While indexing is off it posts
    nothing and purges the unindexable chunks itself; the first sweep after
    re-enable catches up."""
    db = await get_db()
    model = await _indexing_model(db)
    posted = purged = 0
    if model is None:
        for user_id, project_id in sorted(await _index_pairs(db)):
            purged += await _purge_unindexable(db, user_id, project_id)
    else:
        for user_id, project_id, _, _ in await pairs_with_work(db, model):
            await schedule_chat_index(user_id, project_id)
            posted += 1
    if posted or purged:
        logger.info("chat index sweep: posted %d pairs, purged %d chunks", posted, purged)
