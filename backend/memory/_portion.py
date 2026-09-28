"""The portion queue: walk a host's references oldest-first, one at a time.

Leaf of task_builder: the queue cursor (peek/skip/stamp), progress + archived
counts, the per-reference state classifier, and _next_portion. The host is
passed in (db is a param) — no project/run coupling here.
"""
from __future__ import annotations

import logging

from memory._window import slice_windows, window_count

logger = logging.getLogger(__name__)


# `processing_status` classes over a reference's queue position (schema vocabulary:
# queued | processing | ready | error | failed | needs_reindex).
# - _WAITING: transcription is still running — a WAIT, never a completion.
# - _FAILED: the material will never arrive — report unusable, stamp skipped, move on.
_WAITING_STATUSES = frozenset({"queued", "processing"})
_FAILED_STATUSES = frozenset({"error", "failed"})



async def _stamp_consumed(db, reference_id: str, run_id: str) -> None:
    """Mark a reference consumed by `run_id` — the run cursor.

    # ARCH: "the stamp is written when the agent says it is done" —
    # `next_reference` stamps AFTER the verdicts for that reference were applied, so
    # a stamp never precedes the apply that justifies it. The accepted crash window is
    # the other direction: applied, not stamped ⇒ re-served on the next run, where the
    # new facts meet their own twins as merge candidates — a merge, not a duplicate.
    """
    await db.query(
        "UPDATE type::record('documents', $id) SET mem_consolidated_at = time::now(), "
        "mem_consolidated_run = $run, mem_window_done = NONE",
        {"id": reference_id, "run": run_id},
    )


async def _advance_window(
    db, reference_id: str, expected_done: int, next_index: int,
) -> bool:
    """Conditionally advance the window cursor from `expected_done` to `next_index` —
    one WINDOW consumed, more to come. NOT a consume: `mem_consolidated_at` stays NONE so
    the reference remains the oldest unconsumed and the next portion serves its next
    window of the SAME row.

    A compare-and-set: the UPDATE lands only while `mem_window_done` is STILL
    `expected_done` (a fresh reference's NONE reads as 0) AND the reference is not yet
    consumed. Returns True iff the cursor moved — the caller surfaces that as `stamped`.
    This is what makes a concurrent pair of `next_reference` calls safe: two advances
    from the same window cannot both land (the second's WHERE fails once the first moved
    the cursor), so the cursor advances by exactly one, never two; and an advance cannot
    overwrite a consume (`mem_consolidated_at IS NONE` in the WHERE). Reached only after
    the order guard and the idempotency check, so a False here means a concurrent call
    moved first — a safe no-op (the next portion re-serves the current window).
    """
    rows = await db.query(
        "UPDATE type::record('documents', $id) SET mem_window_done = $next "
        "WHERE mem_consolidated_at IS NONE AND (mem_window_done ?? 0) = $expected "
        "RETURN AFTER",
        {"id": reference_id, "expected": expected_done, "next": next_index},
    )
    return bool(rows)


async def _clear_consumed_stamps(db, host_id: str) -> int:
    """Clear the run cursor on every reference under `host_id` that has ANY progress —
    the inverse of `_stamp_consumed` / `_advance_window`, reopening the material for
    another walk.

    Returns the count of references whose cursor was reset. Writes `NONE` to all THREE
    cursor fields (`mem_consolidated_at`, `mem_consolidated_run`, `mem_window_done`) and
    nothing else: no accent, no re-run history (how an extraction was steered is a
    property of the run, not of the reference), and no touch on the fact documents (this
    clears reference CURSOR stamps only; a fact's provenance is its own).

    "Any progress" is `mem_consolidated_at IS NOT NONE OR mem_window_done IS NOT NONE`:
    a partially-walked reference (window cursor advanced, not yet consumed) has cursor
    state too, and leaving its `mem_window_done` standing while clearing the stamps would
    resume it mid-way and silently drop the early windows (Risk #1). For data predating
    windows, `mem_window_done` is always NONE, so the OR is a no-op and the count is
    unchanged — the broadened condition only adds partially-walked references.

    # INVARIANT: clearing is NOT progress — the anti-runaway budget must not zero on a
    # clear.
    # Why: the budget zeroes only on `stamped == True` from `next_reference`; this is the
    # inverse of a stamp, and a tool that can un-stamp is exactly the shape that could
    # manufacture progress forever. The clear stays a plain DB write returning a count —
    # never a portion carrying `stamped`.
    #
    # WHY this leaf writes NOTHING to the event bus: `reprocess_reference` emits
    # `reference_updated` (files_service) because the reference's TEXT changes and clients
    # must refetch; a clear changes only the run CURSOR (`mem_consolidated_at` /
    # `mem_window_done`), and no UI renders the stamp (the parent plan refused a stamp
    # indicator), so there is nothing for a client to refetch and the silence is correct
    # — not an oversight. The day a UI shows the cursor, the emit must be wired HERE (the
    # single write point both apply paths funnel through), mirroring `reprocess_reference`'s
    # core emit.
    """
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE parent_id = $pid "
        "AND is_reference = true AND deleted_at IS NONE "
        "AND (mem_consolidated_at IS NOT NONE OR mem_window_done IS NOT NONE)",
        {"pid": host_id},
    )
    await db.query(
        "UPDATE documents SET mem_consolidated_at = NONE, mem_consolidated_run = NONE, "
        "mem_window_done = NONE WHERE parent_id = $pid AND is_reference = true "
        "AND deleted_at IS NONE "
        "AND (mem_consolidated_at IS NOT NONE OR mem_window_done IS NOT NONE)",
        {"pid": host_id},
    )
    return len(rows or [])



async def _progress(db, host_id: str) -> dict:
    """`{done, total, remaining, archived, windows}` over the host's references.

    `total`/`done`/`remaining` cover the LIVE references (archived excluded — archiving
    is a deliberate user act, `references.py:122`); `archived` counts the excluded ones
    so an empty live queue can say WHY it is empty rather than reading as "finished".
    The chip and the closing report both say "3 of 6" from the first three numbers.

    `windows` reports windows ALONGSIDE references so a long run over a few multi-window
    references does not read "0 of 1" while walking:
    `total` sums `window_count` over the live references, `done` counts consumed windows
    (a fully consolidated reference's whole count + each in-flight reference's
    `mem_window_done`). Computed from content LENGTHS (`string::len`) so the bodies are
    never transferred — the same cost class as the reference counts.
    """
    rows = await db.query(
        "SELECT mem_consolidated_at, mem_window_done, archived, "
        "string::len(content ?? \"\") AS clen FROM documents "
        "WHERE parent_id = $pid AND is_reference = true AND deleted_at IS NONE",
        {"pid": host_id},
    )
    live = [r for r in (rows or []) if not r.get("archived")]
    archived = len(rows or []) - len(live)
    total = len(live)
    done = sum(1 for r in live if r.get("mem_consolidated_at") is not None)
    win_total = sum(window_count(r.get("clen") or 0) for r in live)
    win_done = 0
    for r in live:
        if r.get("mem_consolidated_at") is not None:
            win_done += window_count(r.get("clen") or 0)
        elif r.get("mem_window_done"):
            win_done += r["mem_window_done"]
    return {
        "done": done, "total": total, "remaining": total - done,
        "archived": archived, "windows": {"done": win_done, "total": win_total},
    }



async def _archived_references(db, host_id: str) -> list[dict]:
    """The `[{id, title}]` of the host's archived references — NAMED, so an empty run
    can report WHICH references it excluded (archiving is deliberate but silent in the
    queue; the Семинар run consolidated nothing because both its references were
    archived, and the payload never said so)."""
    rows = await db.query(
        "SELECT meta::id(id) AS id, title, created_at FROM documents "
        "WHERE parent_id = $pid AND is_reference = true AND deleted_at IS NONE "
        "AND archived = true ORDER BY created_at ASC",
        {"pid": host_id},
    )
    return [{"id": r["id"], "title": r.get("title") or ""} for r in (rows or [])]



def _portion_outcome(*, complete: bool, total: int) -> str | None:
    """The terminal outcome of a portion, discriminating a finished run from an empty
    one. `complete` keeps its meaning (no reference, not waiting); `outcome` names
    WHICH: `"complete"` when live material was consolidated (total > 0), or
    `"nothing_to_do"` when there was none (total == 0). `None` while the run is
    in-flight (a reference is being worked or a transcription is pending)."""
    if not complete:
        return None
    return "nothing_to_do" if total == 0 else "complete"



def _reference_state(ref: dict) -> str:
    """One reference's queue position: `waiting` | `unusable` | `ready`.

    `waiting` — transcription still running (queued/processing): a WAIT, never a
    completion and never a stamp. `unusable` — the material will never be readable
    (failed status, or empty text). `ready` — readable material.
    """
    status = ref.get("processing_status")
    if status in _WAITING_STATUSES:
        return "waiting"
    if status in _FAILED_STATUSES or not (ref.get("content") or "").strip():
        return "unusable"
    return "ready"



def _serve_window(ref: dict) -> tuple[str, dict]:
    """The window to serve from a READY `ref`, and its `{index, total, from, to}`.

    `content` is sliced on the way OUT, RAW (no strip): window `mem_window_done` (0 when
    NONE) of the reference, per `slice_windows`. Raw keeps the window COUNT identical to
    `_progress` (which reads `string::len(content)`) and to the bench oracle (`bench_arm`
    sliced raw) — stripping here would make `progress.windows.total` and the served total
    diverge by a window on material with large leading/trailing whitespace. A
    single-window reference is served whole (passthrough). The returned text IS the
    window, so the merge-candidate embed (which reads `reference.content`) matches the
    CURRENT window — a fact straddling a seam meets its twin in both windows and lands as
    a merge candidate, never a forked twin.

    `idx` is clamped to the last window: an unconsumed reference never has the cursor
    past its last window (consume clears `mem_window_done`), so the clamp is a guard
    against corrupt state, not a normal path.
    """
    body = ref.get("content") or ""
    wins = slice_windows(body)
    idx = min(ref.get("mem_window_done") or 0, len(wins) - 1)
    a, b, text = wins[idx]
    return text, {"index": idx, "total": len(wins), "from": a, "to": b}



async def _peek_unconsumed(db, host_id: str) -> list[dict]:
    """The oldest unconsumed reference under `host_id` — empty when the queue is done.

    Carries `mem_window_done` so the caller can serve the right WINDOW of a partially-
    walked reference (a reference is unconsumed for as long as `mem_consolidated_at` is
    NONE, even while its window cursor is mid-walk).
    """
    return await db.query(
        "SELECT meta::id(id) AS id, title, content, media_type, "
        "processing_status, mem_window_done, created_at FROM documents "
        "WHERE parent_id = $pid AND is_reference = true AND mem_consolidated_at IS NONE "
        "AND deleted_at IS NONE AND archived != true ORDER BY created_at ASC LIMIT 1",
        {"pid": host_id},
    )



async def _skip_unusable(db, ref: dict, run_id: str, skipped: list[dict]) -> None:
    """Stamp a reference as consumed-but-unreadable and record it for the report.

    `_stamp_consumed` is what makes the next `_peek_unconsumed` skip it — the skip
    can never loop on the same row.
    """
    await _stamp_consumed(db, ref["id"], run_id)
    logger.warning(
        "consolidate_memory: reference %s %r reported unusable and stamped "
        "skipped — status=%r, %d chars",
        ref["id"], (ref.get("title") or "")[:60],
        ref.get("processing_status"), len((ref.get("content") or "").strip()),
    )
    skipped.append({
        "id": ref["id"], "title": ref.get("title") or "",
        "status": ref.get("processing_status"),
    })



def _portion_result(
    *, reference=None, waiting: bool = False, waiting_reference=None,
    waiting_status=None, skipped: list[dict] | None = None,
) -> dict:
    """The uniform portion-result shape shared by every branch of `_next_portion`."""
    return {
        "reference": reference, "waiting": waiting,
        "waiting_reference": waiting_reference, "waiting_status": waiting_status,
        "unusable_skipped": skipped or [],
    }


async def _next_portion(db, host_id: str, run_id: str) -> dict:
    """The next portion of material under `host_id`, or the reason there is none.

    Returns `{reference, waiting, waiting_reference, waiting_status,
    unusable_skipped}`. `reference` — the oldest unconsumed, READABLE reference;
    `waiting` — the oldest remaining is still transcribing (a WAIT, never a
    completion, never stamped); `unusable_skipped` — references stamped-skipped as
    unreadable (failed status or empty text), so they stop blocking the queue forever.
    `reference is None and not waiting` — nothing left (complete).

    # INVARIANT: the queue walks OLDEST FIRST — a transcribing reference is never skipped
    # past.
    # Why: portions reference each other in sequence, so reading part 4 before part 3
    # exists would attach a dangling reference; a transcribing predecessor blocks the queue
    # until it lands. The "wait" branch distinguishes "nothing left" from "nothing READY
    # yet" — `processing_status` gating must never empty the queue silently.
    """
    skipped: list[dict] = []
    while True:
        rows = await _peek_unconsumed(db, host_id)
        if not rows:
            return _portion_result(skipped=skipped)
        ref = rows[0]
        state = _reference_state(ref)
        if state == "waiting":
            return _portion_result(
                waiting=True, waiting_reference=ref["id"],
                waiting_status=ref.get("processing_status"), skipped=skipped,
            )
        if state == "unusable":
            await _skip_unusable(db, ref, run_id, skipped)
            continue
        content, window = _serve_window(ref)
        return _portion_result(
            reference={
                "id": ref["id"], "title": ref.get("title") or "", "content": content,
                "window": window, "media_type": ref.get("media_type"),
                "processing_status": ref.get("processing_status"),
            },
            skipped=skipped,
        )
