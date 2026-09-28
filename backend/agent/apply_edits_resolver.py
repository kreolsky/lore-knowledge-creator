"""Lock-free edit-resolve/validate/apply core for agent document edits.

Part of the chat-agent-mode system. Extracted from tool_api_surface
 : the all-or-nothing batch resolve→splice→
checkpoint→write core (`_resolve_validate_apply_edits`) + the pinned-region
containment exception (`RegionContainmentReject`), used by the direct apply path
(tool_api_surface.apply_edits_to_document). Internal extraction — no SYSTEM marker.

WHY (imports): live state and the surgical convergence path come from
`agent.doc_state`, the checkpoint helper from `agent.collab_writes`; both are
reached as module attributes at call time so a test patches the owning module.

This module is also the home of the pinned-region containment gate
(`region_containment_error`); the typed post-write failure
(`AppliedUnverifiedError`) lives in `textmatch` beside the primitive that
raises it.
"""
import logging

import settings
from fastapi import HTTPException
from textmatch import AppliedUnverifiedError

from agent import collab_writes, doc_state
from agent.edit_primitives import (
    _splice_edit,
    align_block_boundaries,
    already_applied,
    edit_miss_detail,
    fold_new_string,
    resolve_edit_range,
)
from models import RegionRef

logger = logging.getLogger(__name__)


class RegionContainmentReject(Exception):
    """Raised by the shared apply core when a resolved edit range falls outside the
    session's pinned region. Carries the actionable detail; the caller maps it to a
    409 rejection."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def region_containment_error(
    *,
    edit_from: int,
    edit_to: int,
    edit_doc_id: str,
    has_region: bool,
    region: RegionRef | dict | None,
) -> str | None:
    """Return a human/agent-facing detail string if a resolved edit escapes the
    pinned region; None when it is fully inside (or no region is pinned).

    The authoritative containment gate for pinned-region sessions. Pure so it is
    unit-testable independently of the apply path.

    # INVARIANT: fail-closed — a pinned session whose apply request omits the
    # region is REJECTED (inclusive containment on from/to_cp + doc_id).
    # Why: under a pin the agent may only edit inside the region; the frontend-owned
    # RelativePosition may be lost on a cross-device reload, and a missing region is
    # rejected fail-closed rather than letting an unbounded edit slip through.
    """
    if not has_region:
        return None  # not pinned → unconstrained
    if region is None:
        return "region unavailable — re-pin or unpin before editing"
    r_doc = region.get("doc_id") if isinstance(region, dict) else region.doc_id
    r_from = region.get("from_cp") if isinstance(region, dict) else region.from_cp
    r_to = region.get("to_cp") if isinstance(region, dict) else region.to_cp
    if r_doc != edit_doc_id:
        return "edit targets a different document than the pinned region"
    if r_from <= edit_from and edit_to <= r_to:
        return None
    return "edit falls outside the pinned fragment"


def _classify_edit_miss(
    current_content: str, index: int, old_string: str, new_text: str, rng: str,
) -> dict | None:
    """Handle a resolver miss for edit `index`: an already-applied edit comes back
    as a skip entry; every other miss raises the enriched 409 (or 422).

    # Idempotent-skip probe: a not_found edit whose new_string is present exactly
    # once (same fold the resolver uses) was already applied — drop it and report.
    # Absent/ambiguous/empty-new still raise the enriched 409 (#3) — a wrong
    # old_string is surfaced, never silently swallowed. full_rewrite/ambiguous
    # from the resolver never skip (structural errors).

    # WHY: full_rewrite is NOT a stale/re-read condition — it is a
    # structurally-oversized request. Return 422 (its own next_action "split
    # into smaller edits") instead of 409. Why: 409's next_action is "re-read
    # then retry", which loops a weak model back into re-sending the same
    # whole-document old_string.
    """

    if rng == "not_found":
        verdict, cp = already_applied(current_content, old_string, new_text)
        if verdict == "applied":
            return {"index": index, "at_cp": cp, "reason": "already_applied"}
        tag = "ambiguous" if verdict == "ambiguous" else rng
    else:
        tag = rng
    status = 422 if tag == "full_rewrite" else 409
    raise HTTPException(
        status_code=status,
        detail={"index": index, "error": edit_miss_detail(current_content, old_string, tag)},
    )


def _prepare_edit_text(
    current_content: str, from_cp: int, to_cp: int, new_text: str, folded: bool,
) -> tuple[int, int, str, str]:
    """Fold-aware text shaping: normalize a folded new_string, then align block
    boundaries. Returns `(from_cp, to_cp, new_text, resolved_old)` — the final
    range and text every later gate and the splice see.

    # WHY: old_string and new_string are folded TOGETHER or not at all.
    # Why: asymmetric fold is the doc-4da09e92 incident. Re-normalize after the
    # fold: line-anchored list-spacing fixes only see line starts once the
    # escapes became real newlines.

    # Block-boundary alignment (incident doc-53e340da): a block-level
    # replacement must not be spliced onto the tail of a neighboring line. Runs
    # BEFORE the region gate and the overlap guard so both see the final range;
    # it only ever grows the range over a spaces/tabs run, so non-overlap holds.
    # See the INVARIANT on align_block_boundaries.
    """
    from markdown_normalize import normalize_list_spacing

    if folded:
        new_text = normalize_list_spacing(fold_new_string(new_text))
    from_cp, to_cp, new_text = align_block_boundaries(
        current_content, from_cp, to_cp, new_text,
    )
    return from_cp, to_cp, new_text, current_content[from_cp:to_cp]


def _resolve_one_edit(
    current_content: str, index: int, e: dict, region_check,
    full_rewrite_fraction: float,
) -> tuple[str, dict]:
    """Resolve ONE edit against the snapshot: `("resolved", entry)` or
    `("skip", entry)`; raises HTTPException / RegionContainmentReject.

    # WHY: verify/splice against the RESOLVED slice, not the raw old_string.
    # Why: the fold returns the ORIGINAL range, so raw old_string would make
    # _splice_edit spuriously report "stale" (see the single-edit note).
    """

    old_string = e["old_string"]
    new_text = e["new_string"]
    rng = resolve_edit_range(
        current_content, old_string, full_rewrite_fraction=full_rewrite_fraction,
    )
    if isinstance(rng, str):
        skip = _classify_edit_miss(current_content, index, old_string, new_text, rng)
        return "skip", skip
    from_cp, to_cp, folded = rng
    from_cp, to_cp, new_text, resolved_old = _prepare_edit_text(
        current_content, from_cp, to_cp, new_text, folded,
    )
    # Pinned-region containment gate (confirm path only). The resolved range must
    # lie fully inside the request's region; fail-closed if pinned but no region.
    if region_check is not None:
        containment_err = region_check(from_cp, to_cp)
        if containment_err is not None:
            raise RegionContainmentReject(containment_err)
    # _splice_edit performs the stale-check (verify current[from:to] == resolved_old)
    # before mutating; its new_content is unused (the surgical path uses cp/new_text).
    _new_content, err = _splice_edit(
        content=current_content, from_cp=from_cp, to_cp=to_cp,
        original_text=resolved_old, new_text=new_text,
    )
    if err == "stale":
        raise HTTPException(
            status_code=409,
            detail={"index": index, "error": "Selection no longer matches the document"},
        )
    # Per-edit no-op (old == new): the doc already has the desired state →
    # treat as already-applied skip (observable, no checkpoint contribution).
    if new_text == resolved_old:
        return "skip", {"index": index, "at_cp": from_cp, "reason": "already_applied"}
    return "resolved", {
        "from_cp": from_cp, "to_cp": to_cp, "new_text": new_text,
        "original_text": resolved_old, "index": index,
    }


def _validate_edits_against_snapshot(
    current_content: str, edits: list[dict], region_check,
    full_rewrite_fraction: float,
) -> tuple[list[dict], list[dict]]:
    """The validation pass: resolve every edit against the ONE snapshot —
    `(resolved, skipped)`; one unresolved edit raises and nothing is written."""
    resolved: list[dict] = []  # {from_cp, to_cp, new_text, original_text, index}
    skipped: list[dict] = []   # {index, at_cp, reason}
    for index, e in enumerate(edits):
        kind, entry = _resolve_one_edit(
            current_content, index, e, region_check, full_rewrite_fraction,
        )
        if kind == "skip":
            skipped.append(entry)
        else:
            resolved.append(entry)
    return resolved, skipped


def _reject_overlapping_edits(resolved: list[dict]) -> None:
    """The overlap guard: resolved ranges must be non-overlapping (independent).

    # Mirrors the table-cell batch whose cells are independent. Two edits hitting
    # the same region are rejected (the surgical descending-offset apply assumes
    # non-overlapping ranges; an overlap would corrupt offsets). Good practice:
    # batch edits target independent regional passages (stated in the tool desc).
    """

    if len(resolved) <= 1:
        return
    check = sorted(resolved, key=lambda r: r["from_cp"])
    for i in range(len(check) - 1):
        a, b = check[i], check[i + 1]
        if a["to_cp"] > b["from_cp"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "overlapping edits — batch edits must target independent, "
                             "non-overlapping regions",
                    "indices": [a["index"], b["index"]],
                },
            )


async def _apply_resolved_edits(
    doc_id: str, project_id: str, current_content: str, current_tables_json,
    resolved: list[dict], skipped: list[dict], on_commit,
) -> dict:
    """The apply pass: ONE checkpoint (applied >= 1), the mark-first seam, then
    descending splices. Returns the caller's result dict; maps a post-mark write
    failure to AppliedUnverifiedError (see INVARIANT(data-loss) on the core)."""
    preview = ", ".join(
        r["original_text"][:40] + ("…" if len(r["original_text"]) > 40 else "")
        for r in resolved
    )[:80]
    await collab_writes._create_agent_pre_edit_checkpoint(
        document_id=doc_id, content=current_content,
        tables_json=current_tables_json, original_preview=preview,
    )
    # Seam: mark-first (confirm path). BETWEEN checkpoint and the CRDT write so a
    # mid-apply crash leaves the row applied, never a double-apply. Auto path: no-op.
    if on_commit is not None:
        await on_commit()
    # Apply in DESCENDING from_cp order so each surgical splice's earlier offsets
    # stay valid (earlier text unshifted) under the ONE _write_lock → ONE Y.Doc
    # update → ONE publish (route_document_edits, audit F5).
    ordered = sorted(resolved, key=lambda r: r["from_cp"], reverse=True)
    try:
        await doc_state.route_document_edits(
            doc_id=doc_id, project_id=project_id,
            edits=[{"from_cp": r["from_cp"], "to_cp": r["to_cp"],
                    "new_text": r["new_text"], "original_text": r["original_text"]}
                   for r in ordered],
        )
    except AppliedUnverifiedError:
        raise
    except Exception:
        # Only the confirm path (on_commit) marked applied_at before this write; there
        # the row IS applied even on failure, so surface AppliedUnverifiedError (reload
        # shows applied). The auto path (no mark) lets the original error propagate.
        if on_commit is not None:
            logger.exception("Content mutation failed after mark-first for %s", doc_id)
            raise AppliedUnverifiedError()
        raise
    return {
        "applied": len(resolved), "skipped": skipped, "checkpoint": True,
        "applied_ranges": [r["from_cp"] for r in resolved],
    }


async def _resolve_validate_apply_edits(
    *,
    doc_id: str,
    edits: list[dict],
    project_id: str,
    on_commit=None,
    region_check=None,
) -> dict:
    """Lock-free core used by the direct apply path (apply_edits_to_document).
    Callers MUST hold the per-doc edit lock around this call.

    # INVARIANT(data-loss): an edit_document batch applies all-or-nothing (one
    # unresolved edit raises and nothing is written), over non-overlapping ranges
    # spliced in DESCENDING from_cp order. Why: partial / overlapping / unsorted apply
    # silently drops or corrupts edits (No-silent-degradation) and desyncs the resume
    # trace. This is the single home of the batch-apply contract; both callers reach it.

    `edits` are pre-normalized {old_string, new_string}; `on_commit` /
    `region_check` are the confirm path's seams (see the helper contracts).
    Returns {applied, skipped, checkpoint, noop?}. Raises HTTPException (stale/
    miss/overlap/full_rewrite) or RegionContainmentReject; when on_commit ran and
    the write then fails, re-raises AppliedUnverifiedError (mark-first: applied)."""
    # WHY (agent source): resolve current content AND tables from the
    # Y.Doc via the shared helper, NOT merge_live_content. Why: merge_live_content
    # returns the derived GFM read-model, so splicing against it persists a
    # GFM-expanded body — see resolve_live_doc_state.
    current_content, current_tables_json = await doc_state.resolve_live_doc_state(doc_id)

    full_rewrite_fraction = await settings.get("AGENT_FULL_REWRITE_FRACTION")
    resolved, skipped = _validate_edits_against_snapshot(
        current_content, edits, region_check, full_rewrite_fraction,
    )
    _reject_overlapping_edits(resolved)

    # ── All-skipped short-circuit: NO checkpoint (History stays clean) ──
    # Mirrors the single-edit noop short-circuit and the table-cell batch noop.
    if not resolved:
        if on_commit is not None:
            await on_commit()
        return {"noop": True, "applied": 0, "skipped": skipped, "checkpoint": False}

    return await _apply_resolved_edits(
        doc_id, project_id, current_content, current_tables_json,
        resolved, skipped, on_commit,
    )
