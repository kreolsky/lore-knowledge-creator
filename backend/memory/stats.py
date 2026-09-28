"""Project-wide memory shape — the watchdog numbers a run reports about itself.

# ARCH: these numbers are computed SERVER-side and returned in the apply result, so
# the agent's closing report is composed from what the server did rather than from
# what the agent recalls doing. The distinction is load-bearing because there is no
# pending state: the report is the only place a run's outcome outlives the transcript,
# and a recalled report is exactly as reliable as the recollection.

# ARCH: memory — stats. There is one identity level, so the watchdog reports facts
# (live + retired) and nothing else: no split ratio and no graph shape, because a
# flat fact model has neither.
"""
from __future__ import annotations

import logging

from db import get_db

logger = logging.getLogger(__name__)


async def memory_shape(project_id: str) -> dict:
    """One scan over the project's memory facts → `{facts, retired, by_reference}`.

    The CANONICAL memory scan: `memory_stats` and `facts_by_reference` both derive from
    it (one scan, no per-consumer queries that could drift).
    `facts`/`retired` mirror `memory_stats`; `by_reference` is the live-facts-per-
    reference map (provenance-sourced) — a merged fact citing two references is counted
    under BOTH (it carries both in provenance), so the map can sum above the live count,
    which is attestation, not a bug.

    # ARCH: the apply hot path calls this directly so a batch reports `{facts, retired}`
    # AND the just-consolidated reference's count from a SINGLE round-trip, not two.
    """
    db = await get_db()
    # WHY: this scan does NOT filter `deleted_at` like every reader does —
    # `mem_active` is the LABEL that keeps a merge-retirement countable under `retired`.
    # Why: stats report the memory's shape INCLUDING its retired share (the
    # "retired facts still in this project" sweep axis idx_documents_memory indexes);
    # filtering deleted_at here would silently zero `retired` the moment retirement
    # sets it. A fact the USER deleted (deleted_at set, mem_active still true) counts
    # nowhere — it is invisible everywhere else and is not a retirement.
    rows = await db.query(
        "SELECT mem_active, deleted_at, mem FROM documents WHERE project_id = $pid "
        "AND is_memory = true",
        {"pid": project_id},
    )
    facts = 0
    retired = 0
    by_reference: dict[str, int] = {}
    for r in (rows or []):
        if r.get("mem_active") is False:
            retired += 1
            continue
        if r.get("deleted_at"):
            continue
        facts += 1
        srcs = ((r.get("mem") or {}).get("provenance") or {}).get("sources") or []
        for s in srcs:
            if isinstance(s, dict) and s.get("kind") == "reference" and s.get("id"):
                key = str(s["id"])
                by_reference[key] = by_reference.get(key, 0) + 1
    return {"facts": facts, "retired": retired, "by_reference": by_reference}


async def memory_stats(project_id: str) -> dict:
    """`{facts, retired}` over the project's whole memory space (a projection of
    `memory_shape`).

    `facts` counts LIVE facts (soft-delete-free); `retired` counts superseded/
    merged-away facts — soft-deleted by retirement, kept for link stability (D2)
    but no longer served or searched, and distinguished from a user deletion by
    the `mem_active` label. A growing `retired` share is normal accumulation
    (corrections and merges), not a quality signal on its own.
    """
    s = await memory_shape(project_id)
    return {"facts": s["facts"], "retired": s["retired"]}


async def facts_by_reference(project_id: str) -> dict[str, int]:
    """Live fact counts keyed by the reference id each fact cites in provenance
    (the `by_reference` projection of `memory_shape`).

    # ARCH: facts-per-reference is the extraction quota's signal. It is computed
    # from provenance — the one thread from a fact back to the material it came from —
    # so it survives resume and needs no run state: the count is what the project's
    # memory actually holds, not what any one run recalls writing.
    """
    return (await memory_shape(project_id))["by_reference"]


def log_memory_shape(where: str, project_id: str, stats: dict) -> None:
    """Record the project's memory shape.

    Both ends of a run call this — the payload build and the apply — so the shape is
    in the log whether or not the run got as far as writing anything.
    """
    logger.info(
        "%s: project %s — %d facts, %d retired",
        where, project_id, stats.get("facts", 0), stats.get("retired", 0),
    )


__all__ = [
    "log_memory_shape", "memory_shape", "memory_stats", "facts_by_reference",
]
