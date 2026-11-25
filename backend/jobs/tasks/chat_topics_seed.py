"""Chat topics — a branch's toc seeded from its source's, and the toc upsert both paths share.

# WHY a module of its own: seeding is the one chat-topics step with no model
# call; save_toc lives here so chat_topics imports it without a cycle.
"""
from __future__ import annotations

import logging

from chat_sessions.conversation import origin_of

logger = logging.getLogger("jobs.tasks.chat_topics")


async def save_toc(db, chat: dict, user_id: str, project_id: str, model: str,
                   toc: dict, exchanges: list[dict]) -> None:
    """Upsert the toc; labelled_hashes = exchange_hash of each labelled id, aligned."""
    hashes = {e["id"]: e["hash"] for e in exchanges}
    await db.query(
        "UPSERT type::record('chat_tocs', $cid) CONTENT $row",
        {"cid": chat["sid"], "row": {
            "user_id": user_id, "project_id": project_id, "chat_id": chat["sid"],
            "model": model,
            "frozen_ids": toc["frozen_ids"], "labelled_ids": toc["labelled_ids"],
            "labelled_hashes": [hashes[m] for m in toc["labelled_ids"]],
            "topics": toc["topics"],
            "version": toc.get("version") or 1,
        }},
    )


def seed_toc(source: dict, origins: dict[str, str], exchanges: list[dict]) -> dict | None:
    """A branch's toc from its source's stored toc, or None when nothing maps.

    A source labelled id maps to the branch exchange whose user row has the
    same origin (`origins` = source id → origin_of) AND whose exchange_hash
    equals the source's STORED hash for it; a topic with an unmapped id is
    dropped (prune_toc's rule). `exchanges` = the branch's light exchanges.

    WHY the stored hash and not the source's verify: the source is not
    labelled once the branch is its thread's newest, so an edit made to it
    before the fork may not be pruned yet — hashes recomputed from the branch
    would bless a copied synopsis of edited-away text (see the
    INVARIANT(security) in verify_tocs)."""
    by_origin = {origin_of(e["user"]): e for e in exchanges}
    labelled = source.get("labelled_ids") or []
    stored = dict(zip(labelled, source.get("labelled_hashes") or []))
    mapped: dict[str, dict] = {}
    for m in labelled:
        ex = by_origin.get(origins.get(m, ""))
        if ex is not None and m in stored and ex["hash"] == stored[m]:
            mapped[m] = ex
    topics = [{**t, "message_ids": [mapped[m]["id"] for m in t["message_ids"]]}
              for t in source.get("topics") or [] if all(m in mapped for m in t["message_ids"])]
    if not topics:
        return None
    kept = {m for t in topics for m in t["message_ids"]}
    by_id = {e["id"]: e for e in exchanges}
    ids = [e["id"] for e in exchanges if e["id"] in kept]
    frozen = {mapped[m]["id"] for m in source.get("frozen_ids") or [] if m in mapped}
    return {"topics": topics, "labelled_ids": ids,
            "labelled_hashes": [by_id[m]["hash"] for m in ids],
            "frozen_ids": [m for m in ids if m in frozen], "model": source.get("model"),
            "version": source.get("version") or 1}


async def seed_branch_tocs(db, user_id: str, project_id: str, chats: list[dict]) -> int:
    """Seed every labelled branch with no toc whose source has one (seed_toc);
    returns how many were seeded. No model call."""
    forks = {c["sid"]: c for c in chats if c.get("forked_from")}
    if not forks:
        return 0
    params = {"uid": user_id, "pid": project_id}
    have = await db.query(
        "SELECT chat_id FROM chat_tocs WHERE user_id = $uid AND project_id = $pid "
        "AND chat_id IN $ids", {**params, "ids": sorted(forks)},
    ) or []
    for r in have:
        forks.pop(r["chat_id"], None)
    if not forks:
        return 0
    # WHY the user filter on the source too: a toc is its owner's labelling;
    # a branch is only ever seeded from a toc of the same user.
    sources = {t["chat_id"]: t for t in await db.query(
        "SELECT chat_id, labelled_ids, labelled_hashes, frozen_ids, topics, model, version FROM chat_tocs "
        "WHERE user_id = $uid AND project_id = $pid AND chat_id IN $ids",
        {**params, "ids": sorted({c["forked_from"] for c in forks.values()})},
    ) or []}
    if not sources:
        return 0
    rows = await db.query(
        "SELECT meta::id(id) AS id, origin_id FROM $ids.map(|$x| type::record('messages', $x))",
        {"ids": sorted({m for t in sources.values() for m in t.get("labelled_ids") or []})},
    ) or []
    origins = {r["id"]: origin_of(r) for r in rows if r.get("id")}
    seeded = 0
    for chat in forks.values():
        source = sources.get(chat["forked_from"])
        toc = seed_toc(source, origins, chat["exchanges"]) if source else None
        if toc is None:
            continue
        await save_toc(db, chat, user_id, project_id, toc["model"], toc, chat["exchanges"])
        seeded += 1
        logger.info("chat topics: toc of branch %s seeded from %s — %d topics, no model call",
                    chat["sid"], chat["forked_from"], len(toc["topics"]))
    return seeded
