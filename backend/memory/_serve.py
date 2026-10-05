"""Serve memory facts on demand (the two-channel fact delivery).

Leaf of task_builder: the memory_index query (live facts only), the served-fact page
(the fact body + revision marker, under a char budget), and the public
get_memory_facts / get_fact_history readers. task_builder re-exports the two
public readers.

# ARCH: memory — serve. A FACT is a document; its `content` IS the fact, so the
# index names facts cheaply (id + title) and the bodies arrive on demand
# via get_memory_facts. Retired facts are SOFT-DELETED (`deleted_at` set by
# retirement) and never enter the index — the shared `deleted_at IS NONE`
# predicate does the hiding; `mem_active` is only the label (stats, index sweep).
"""
from __future__ import annotations

import settings
from fastapi import HTTPException

from db import get_db
from memory.facts import fact_history, fact_revisions


async def _load_memory_index(db, project_id: str) -> list[dict]:
    """Every LIVE fact in the project's ONE memory space — an INDEX only, no bodies.

    # INVARIANT: candidates are NEVER filtered by which target produced them, nor by
    # any subfolder inside `Memory`.
    # Why: that filter is precisely what made a duplicate fact across two targets a
    # certainty rather than a risk. If subfolders are ever introduced inside Memory,
    # this query must keep ignoring them or per-target fragmentation returns under a
    # friendlier name.

    # ARCH (two-channel): the index carries scalar fields only — id and title —
    # and no fact body; bodies arrive on demand via `get_memory_facts`.
    # Shipping the bodies here would pull the whole fact corpus into every portion
    # payload, which is precisely the payload the incremental run exists to keep small.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id, title FROM documents WHERE project_id = $pid "
        "AND is_memory = true AND deleted_at IS NONE",
        {"pid": project_id},
    )
    return [
        {"id": r["id"], "title": r.get("title") or ""}
        for r in (rows or [])
    ]



def _serve_fact(doc: dict) -> dict:
    """One live fact as SERVED: id, title, body, and the revision marker when N > 0.

    The marker is `revisions` (count of prior wordings on the version-history chain) +
    `last_revised_at` (the most recent supersede), attached ONLY when the fact has
    history. The common fact pays nothing — a `revisions: 0` served on every fact is
    per-fetch token cost for a field that never carries information.

    # INVARIANT: the retired TEXT never enters the portion payload — only the count.
    # Why: handing a model back a number it just corrected undoes the correction. The
    # marker says "this was revised"; the retired wording reaches the agent only via
    # `get_fact_history` (the explicit-request channel).
    """
    mem = doc.get("mem") or {}
    served = {
        "id": doc["id"],
        "title": doc.get("title") or "",
        "text": doc.get("content") or "",
    }
    revisions, last_revised_at = fact_revisions(mem)
    if revisions > 0:
        served["revisions"] = revisions
        served["last_revised_at"] = last_revised_at
    return served


async def get_memory_facts(
    *, project_id: str, ids: list[str],
) -> dict:
    """Fact bodies on demand — the second channel of the two-channel delivery.

    # ARCH: the index names every fact and carries no bodies; this fetches the bodies
    # ONLY for the facts the adjudication step is about to look at.

    # ARCH: BUDGETED — a total char ceiling defers the rest to `deferred` WITH their
    # presence noted. Over-budget is always REPORTED — a silent truncation hides a
    # merge candidate. The budget is its OWN knob, never RETRIEVAL_BUDGET_TOKENS_MEMORY
    # (an explicit id-list fetch, not the stage-5 semantic injection).
    """
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, content, mem FROM documents "
        "WHERE project_id = $pid AND meta::id(id) IN $ids "
        "AND is_memory = true AND deleted_at IS NONE",
        {"pid": project_id, "ids": ids},
    )
    by_id = {r.get("id"): r for r in (rows or [])}
    return _page_facts_by_budget(
        ids, by_id, budget_chars=await settings.get("MEMORY_FACTS_PAGE_CHARS"),
    )


def _page_facts_by_budget(
    ids: list[str], by_id: dict[str, dict], *, budget_chars: int,
) -> dict:
    """Walk `ids` in order, serving each fact's body under a per-call char budget.

    Returns `{facts, missing, deferred}`. A fact whose body would push the call over
    MEMORY_FACTS_PAGE_CHARS lands in `deferred` NAMED — never silently dropped (a
    hidden merge candidate is the failure the duplicate gate exists to prevent). The
    first fact is always served even if it alone fills the budget, so an `ids: [one]`
    fetch can never come back empty.

    # WHY: the budget measures the SERVED page.
    # Why: facts are shipped as their body text, so measuring it is measuring the
    # payload that ships.
    """
    facts: list[dict] = []
    deferred: list[dict] = []
    missing: list[str] = []
    total_chars = 0
    for doc_id in ids:
        r = by_id.get(doc_id)
        if r is None:
            missing.append(doc_id)
            continue
        served = _serve_fact(r)
        page_chars = len(served["text"])
        if facts and total_chars + page_chars > budget_chars:
            deferred.append({"id": doc_id, "title": served["title"]})
            continue
        total_chars += page_chars
        facts.append(served)
    return {"facts": facts, "missing": missing, "deferred": deferred}


async def get_fact_history(
    *, project_id: str, fact_id: str,
) -> dict:
    """The version history of one fact — the explicit-request channel.

    # ARCH: the served payload carries only a revision MARKER (a count); this tool
    # returns the retired wording + each predecessor's supersede reason + provenance,
    # so "why did this fact change" is answerable without the retired text riding in
    # every portion. The agent calls it ONLY when a served fact's marker is non-zero.

    # WHY: history is read for a LIVE fact the agent currently sees.
    # Why: a retired fact id (or an unknown one) is a 404, never an empty history that
    # reads as "no history" — an empty result for a fact the marker said was revised
    # makes the tool look broken and the agent re-ask, which is the loop this task
    # exists to end.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, content, mem FROM documents "
        "WHERE project_id = $pid AND meta::id(id) = $fid "
        "AND is_memory = true AND deleted_at IS NONE",
        {"pid": project_id, "fid": fact_id},
    )
    if not rows:
        # Layer (c) of the address-failure contract: the 404 carries the one-turn
        # recovery action instead of leaving the agent to re-ask blindly.
        raise HTTPException(status_code=404, detail=(
            "Fact not found — retired, merged, or unknown. Fetch the live fact via "
            "get_memory_facts / memory_index, then ask for its history"
        ))
    doc = rows[0]
    mem = doc.get("mem") or {}
    history = fact_history(mem)
    return {
        "fact_id": fact_id,
        "title": doc.get("title") or "",
        "text": doc.get("content") or "",
        "revisions": len(history),
        "history": history,
    }
