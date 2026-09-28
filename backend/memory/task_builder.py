"""Building the payload a consolidation portion consumes.

# ARCH: the server hands the agent its material and its dedup candidates; the agent
# judges. Dedup does NOT rest on the agent remembering to look first — that is the
# single property that makes "never duplicate an entity" structural rather than
# instruction-based.

# ARCH: the unit of consolidation is ONE
# reference. The payload carries `{run_id, target, reference, memory_index, progress}`
# — `reference` is singular, and the cursor is a stamp on the reference itself
# (`documents.mem_consolidated_at`). There is deliberately NO run state (design P11:
# the run key row IS the run), so "resume" is the absence of a stamp: a run that dies
# mid-way leaves its references unstamped and the next call continues. This is
# what makes the run incremental — an upload added next month is the only thing a
# later run reads.

This module is the ORCHESTRATOR: target resolution + host resolution, the portion
assembly (_assemble_portion), the run entry points (build_consolidation_task /
next_reference) and the in-order stamp (_stamp_completed). The queue cursor, the
fact-serving readers, and the merge-candidate selection live in the sibling leaves
_portion / _serve / _candidates, re-exported here.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from db import fetch_one, get_db
from memory._candidates import (
    _merge_candidates_for_reference,
)
from memory._portion import (
    _advance_window,
    _archived_references,
    _clear_consumed_stamps,
    _next_portion,
    _peek_unconsumed,
    _portion_outcome,
    _progress,
    _reference_state,
    _stamp_consumed,
)
from memory._serve import _load_memory_index, get_fact_history, get_memory_facts
from memory._window import window_count
from memory.stats import log_memory_shape, memory_stats
from models import is_ref_row

logger = logging.getLogger(__name__)

async def _load_target(project_id: str, target_doc_id: str) -> dict:
    target = await fetch_one("documents", target_doc_id)
    if not target or target.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Target document not found")
    # ARCH: uniform 404 for missing AND cross-project — no existence oracle (mirrors
    # scope.gate_mutation_target).
    if target.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Target document not found")
    return target


def _resolve_material_host(target: dict, target_doc_id: str) -> str:
    """The document whose attached references are this run's material.

    A plain document is its own host. A REFERENCE resolves to its host document, so
    the run covers that host's whole stack of attached material — the open reference
    and all of its siblings.

    # INVARIANT: a reference id is never a material anchor — it re-resolves to its
    # host document (one hop, never further).
    # Why: a reference cannot have children (schema event
    # `documents_reference_parent_check` enforces the other end of the same rule), so
    # `parent_id = <ref>` is empty BY CONSTRUCTION — starting a run from an open
    # reference produced a payload with no material and no error, which reads as
    # "nothing to consolidate". The unit of consolidation is the host document; an
    # open reference only says WHICH document. Same rule the chat context picker
    # already follows on the frontend ("a ref id is not a valid tree anchor",
    # ChatInput.tsx, user rule 2026-07-09). ONE hop: a plain document is used as-is,
    # or "consolidate this document" would silently widen to its parent's neighbours.
    """
    if not is_ref_row(target):
        return target_doc_id
    host = target.get("parent_id")
    if not host:
        # Unreachable through the live schema (the event above rejects it on CREATE
        # and UPDATE) and repaired in legacy data by `rehost_null_parent_references`.
        # Kept as a LOUD failure rather than an empty run: the quiet version is
        # exactly the failure mode this resolver exists to remove.
        raise HTTPException(
            status_code=400,
            detail="This reference has no host document — nothing to consolidate",
        )
    return host


async def resolve_consolidation_host(
    *, project_id: str, target_doc_id: str,
) -> tuple[dict, str]:
    """Validate the target and resolve its material host. NO writes.

    Returns `(target, host_id)` where `target` is the resolved HOST document — a
    reference target re-anchors to the document it hangs on, so the material is that
    document's reference stack (see `_resolve_material_host`).

    # WHY: kept separate from `_assemble_portion` because the route validates the
    # target BEFORE minting the run key — an invalid target must not persist a
    # credential row (uniform 404, no existence oracle).
    """
    target = await _load_target(project_id, target_doc_id)
    host_id = _resolve_material_host(target, target_doc_id)
    if host_id != target_doc_id:
        # The run was started from an open reference: re-anchor onto its host, and
        # re-load so `target` in the payload describes the document being consolidated
        # (the same row the access + cross-project gate already ran on for the ref).
        target = await _load_target(project_id, host_id)
    return target, host_id


async def _assemble_portion(
    db, project_id: str, host_id: str, run_id: str,
    host: dict | None = None, *, stats_at_start: dict | None = None,
) -> dict:
    """One portion payload: target + THIS reference + the index + progress + stats.

    Called by both ends of the loop — `consolidate_memory` (first portion) and
    `next_reference` (after stamping the completed one) — so the two can never drift
    on shape. `complete` is derived: no reference and not waiting; `outcome`
    discriminates a finished run (`complete`) from an empty one (`nothing_to_do`).

    `stats_at_start` is carried ONLY on the first portion (the run's opening baseline,
    so the closing report states a DELTA — never per-portion, or the delta silently
    becomes per-portion). `archived_skipped` names the host's archived references so an
    empty run reports WHY it had nothing, rather than reading as finished.
    """
    if host is None:
        host = await _load_target(project_id, host_id)
    index = await _load_memory_index(db, project_id)
    stats = await memory_stats(project_id)
    log_memory_shape("consolidate_memory", project_id, stats)
    portion = await _next_portion(db, host_id, run_id)
    complete = portion["reference"] is None and not portion["waiting"]
    progress = await _progress(db, host_id)
    merge_candidates = await _merge_candidates_for_reference(
        db, project_id, host_id, portion["reference"],
    )
    reference_shape = None
    if complete:
        reference_shape = await _closing_reference_shape(db, project_id, host_id, run_id)
    payload = {
        "target": {"id": host_id, "title": host.get("title") or ""},
        "reference": portion["reference"],
        "memory_index": index,
        "merge_candidates": merge_candidates,
        "progress": progress,
        "stats": stats,
        "unusable_skipped": portion["unusable_skipped"],
        "waiting": portion["waiting"],
        "waiting_reference": portion["waiting_reference"],
        "waiting_status": portion["waiting_status"],
        "complete": complete,
        "outcome": _portion_outcome(complete=complete, total=progress["total"]),
        "archived_skipped": await _archived_references(db, host_id),
        "reference_shape": reference_shape,
    }
    if stats_at_start is not None:
        payload["stats_at_start"] = stats_at_start
    return payload


async def _closing_reference_shape(
    db, project_id: str, host_id: str, run_id: str,
) -> list[dict]:
    """Per-reference `{reference_id, title, facts, batches}` for every reference under
    the host, oldest-first — the shape the closing report is composed from.

    `facts` is provenance-sourced (`facts_by_reference`); `batches` is the per-run
    counter (`reference_batch_counts`), so a reference that was skipped (no apply)
    reports `batches: null` rather than `0` — null is 'the run never applied to it', a
    distinct shape from 'one apply that wrote nothing'.

    Called only when the run is DONE (one extra query at close). The caller guards with
    an explicit `if`, not a ternary: `await f() if cond else None` would await None
    mid-run, because Python awaits the conditional's RESULT (None when not done)."""
    from memory import run_key
    from memory.stats import facts_by_reference

    rows = await db.query(
        "SELECT meta::id(id) AS id, title, created_at FROM documents "
        "WHERE parent_id = $pid AND is_reference = true AND deleted_at IS NONE "
        "AND archived != true ORDER BY created_at ASC",
        {"pid": host_id},
    )
    facts = await facts_by_reference(project_id)
    batches = await run_key.reference_batch_counts(run_id) or {}
    return [
        {
            "reference_id": r["id"],
            "title": r.get("title") or "",
            "facts": facts.get(r["id"], 0),
            "batches": batches.get(r["id"]),
        }
        for r in (rows or [])
    ]


async def build_consolidation_task(
    *, project_id: str, target_doc_id: str, run_id: str,
) -> dict:
    """The FIRST portion of a consolidation run over `target_doc_id`.

    Returns `{target, reference, memory_index, merge_candidates, progress, stats,
    stats_at_start, unusable_skipped, waiting, waiting_reference, waiting_status,
    complete, outcome, archived_skipped}`; the caller adds `run_id`.

    Candidate selection at run start is the flat index of every live fact — there is
    no type to filter on (the entity level is gone), so the duplicate gate and the
    agent's own comparison do the narrowing. Serving the flat index costs nothing at
    Lore's fact counts and is what stops a premature filter from hiding the very
    duplicate the run should catch.
    """
    db = await get_db()
    target, host_id = await resolve_consolidation_host(
        project_id=project_id, target_doc_id=target_doc_id,
    )
    return await _assemble_portion(
        db, project_id, host_id, run_id, host=target,
        # The opening watchdog stats are the run's baseline — captured once, here,
        # so the closing report can state a delta instead of a project-wide census.
        stats_at_start=await memory_stats(project_id),
    )


async def _stamp_completed(
    db, ref: dict, reference_id: str, host_id: str, run_id: str,
    *, completed_window_index: int | None = None,
) -> bool:
    """Stamp a completed WINDOW (or a single-window reference) as consumed, enforcing
    queue order. Returns True when THIS call moved the cursor (advance or consume), False
    otherwise (idempotent no-op / already consumed) — the agent-loop budget reset keys off
    it (`next_reference` surfaces it as `stamped`).

    Refused: a still-transcribing reference (never served — `_next_portion` reports
    `waiting`), and a reference that is not the oldest unconsumed one under its host (a
    partially-walked reference is still unconsumed, so the in-order guard keeps refusing
    an out-of-order completion unchanged). The window advance/consume/idempotency decision
    is `_decide_window_step`.

    # INVARIANT: the queue is stamped IN ORDER.
    # Why: the stamp is the run cursor and resume is the absence of a stamp — an
    # out-of-order stamp would permanently skip the older material, so the only
    # stateless way to keep the cursor honest is to require the completed reference to
    # BE the next one in the queue.
    """
    if ref.get("mem_consolidated_at") is not None:
        return False
    if _reference_state(ref) == "waiting":
        raise HTTPException(
            status_code=400,
            detail="This reference is still transcribing — it cannot be completed yet",
        )
    oldest = await _peek_unconsumed(db, host_id)
    # `reference_id` (the caller's string) is the comparison operand, NOT `ref["id"]`:
    # fetch_one returns a RecordID while `_peek_unconsumed` projects `meta::id(id)` (a
    # plain string); the two never compare equal, which made the guard refuse every stamp.
    if not oldest or oldest[0]["id"] != reference_id:
        raise HTTPException(
            status_code=400,
            detail="Consolidate references in order — the oldest unconsolidated "
            "reference must be completed first",
        )
    return await _decide_window_step(
        db, ref, reference_id, run_id,
        completed_window_index=completed_window_index,
    )


async def _decide_window_step(
    db, ref: dict, reference_id: str, run_id: str,
    *, completed_window_index: int | None,
) -> bool:
    """The window advance/consume decision, after the order guard has passed.

    A multi-window reference is stamped once per WINDOW: each call advances
    `mem_window_done` (more windows — the reference stays unconsumed and the next portion
    serves its next window), or on the LAST window consumes it via `_stamp_consumed`
    (writes `mem_consolidated_at`, clears `mem_window_done`). A single-window reference is
    consumed in one step (passthrough).

    Idempotency (option B): `completed_window_index` is the window the agent just finished.
    A genuine step has the cursor STILL at that index → advance/consume, return True. A
    retried call after a lost response finds the cursor already past it
    (`done != completed_window_index`) → return False (no-op; the caller re-serves the
    current window), so a lost response can never skip a window. An out-of-range index is
    also `!= done` → a safe no-op. Omitted (MCP/legacy) → unconditional advance (today's
    behaviour, the accepted window-level crash-window).
    """
    n = window_count(len(ref.get("content") or ""))
    done = ref.get("mem_window_done") or 0
    if completed_window_index is not None and done != completed_window_index:
        return False
    if done + 1 >= n:
        await _stamp_consumed(db, reference_id, run_id)
        return True
    return await _advance_window(db, reference_id, done, done + 1)


async def reopen_consolidation(*, project_id: str, target_doc_id: str) -> dict:
    """Clear the consumed-stamps on a target's references so the material can be walked
    again — the inverse of a completed run, at the granularity of the TARGET.

    The unit is the TARGET: a reference id re-resolves to its host and the clear covers
    that host's whole stack (reuse `resolve_consolidation_host`, identical behaviour to
    `consolidate_memory` — an accent applies to the material as a whole, not to the third
    file out of six, so per-id clearing would make the standard case an id-enumeration
    chore). Returns `{target_doc_id, cleared}`.

    Writes `NONE` to both stamp fields and stops. Nothing is recorded about why — how an
    extraction was steered is a property of the run, not of the fact or the reference.
    The clear is the inverse of `_stamp_consumed`; see `_clear_consumed_stamps` for why it
    must never signal progress. Re-extraction is not dirty: a second walk sees the
    existing facts in `memory_index` + `merge_candidates` WITH their bodies, so
    overlapping material lands as `merge` / `supersede` rather than a forked twin.

    # WHY: this entrypoint raises ONLY the resolver's own refusals and ONLY before
    # any write. Why: the chat apply path (`_run_reopen_clear`) wraps the whole call in a
    # single try, mapping an HTTPException → STALE (pre-side-effect refusal, claim
    # reverted) and any other Exception → applied-unverified (post-side-effect failure).
    # That split is sound only if NO HTTPException can escape AFTER the clear has run —
    # i.e. the clear (`_clear_consumed_stamps`, a plain DB write) never raises one. The
    # resolver (`resolve_consolidation_host`) runs before the clear and raises 404
    # (missing / cross-project / deleted — uniform, no existence oracle) and 400 (a
    # hostless reference); those are the complete set of HTTPExceptions this function can
    # raise. Keep the clear HTTPException-free, or the merged try rebrands a clear-time
    # failure as a clean refusal.
    """
    db = await get_db()
    _target, host_id = await resolve_consolidation_host(
        project_id=project_id, target_doc_id=target_doc_id,
    )
    cleared = await _clear_consumed_stamps(db, host_id)
    return {"target_doc_id": host_id, "cleared": cleared}


async def _fetch_completed_ref(
    project_id: str, completed_reference_id: str,
) -> tuple[dict, str]:
    """Load the completed reference, validate it, and return `(ref, host_id)`.

    Uniform 404 for missing / cross-project / deleted (no existence oracle). A
    non-reference is refused: the stamp is the run cursor over MATERIAL and a plain
    document is not part of any host's queue (would lie about progress and put a datetime
    on rows the cursor never selects). A hostless reference is refused loudly — legacy
    data with a null parent is repaired by `rehost_null_parent_references`.
    """
    ref = await fetch_one("documents", completed_reference_id)
    if not ref or ref.get("deleted_at") or ref.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Reference not found")
    if not is_ref_row(ref):
        raise HTTPException(
            status_code=400, detail="Only a reference is stamped as consolidated",
        )
    host_id = ref.get("parent_id")
    if not host_id:
        raise HTTPException(
            status_code=400, detail="This reference has no host document",
        )
    return ref, host_id


async def next_reference(
    *, project_id: str, run_id: str, completed_reference_id: str,
    completed_window_index: int | None = None,
) -> dict:
    """Continue a run: stamp the completed window, serve the next portion.

    `completed_window_index` (the window the agent just finished, from the served
    portion's `reference.window.index`) makes a retried call after a lost response
    idempotent — see `_stamp_completed`. Omitted by callers that do not track windows
    (MCP / legacy), which get unconditional advance.

    # ARCH: everything a portion needs is DERIVABLE — the host is the completed
    # reference's `parent_id`, the project is on the run key row. No run state, no
    # cursor table: a stamp on the reference IS the progress.
    """
    from memory.apply import _resolve_run

    db = await get_db()
    # `_resolve_run` refuses a whole-project key (uniform 404), so `mem_consolidated_run`
    # can never cite a chat session's own key.
    await _resolve_run(project_id, run_id)
    ref, host_id = await _fetch_completed_ref(project_id, completed_reference_id)
    stamped = await _stamp_completed(
        db, ref, completed_reference_id, host_id, run_id,
        completed_window_index=completed_window_index,
    )
    portion = await _assemble_portion(db, project_id, host_id, run_id)
    # `stamped` is the agent-loop PROGRESS signal: True only when THIS call moved the
    # cursor (a window advance or a consume). An idempotent re-stamp serves a fresh-looking
    # portion but is not progress — the anti-runaway budget zeroes only on a True
    # here (see memory/_portion.py), so a runaway that re-passes a stamped id
    # cannot zero the counter forever. See _stamp_completed.
    portion["stamped"] = stamped
    return portion


__all__ = [
    "build_consolidation_task",
    "get_fact_history",
    "get_memory_facts",
    "next_reference",
    "reopen_consolidation",
    "resolve_consolidation_host",
]
