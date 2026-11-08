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
import json
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


def _splice_edge_state(
    content: str, mask: list[bool], from_cp: int, to_cp: int,
) -> tuple[bool, str | None, str | None]:
    """(edge-clean?, prev-before, after-first) for a splice — the fence gate on
    from_cp plus the two surviving neighbour lines the post-splice skip rule
    needs when a converted anchor touches conv_new's first/last line."""
    if from_cp > 0:
        if mask[content.count("\n", 0, from_cp)]:
            return False, None, None  # starts inside a fence of the live content
    prev_before = content[:from_cp].rsplit("\n", 1)[-1] if from_cp > 0 else None
    after_first = content[to_cp:].split("\n", 1)[0] if to_cp < len(content) else None
    return True, prev_before, after_first


def _anchors_own_their_lines(
    conv_new: str, content: str, mask: list[bool], from_cp: int, to_cp: int,
) -> bool:
    """The post-splice skip rule for edit-path table conversion: True when every
    converted anchor in ``conv_new`` (the rewritten new_text) sits on a line of
    its own in the text AS IT WILL STAND — never judged on new_text alone.

    Per anchor line: the neighbour lines must hold no unescaped ``|`` (a bare
    pipe there means a legacy pipe block whose edge rows the conversion would
    orphan), and the splice must not start inside a fenced block of the live
    content (a table authored inside a fence is code).

    An anchor interior to conv_new is already newline-locked on both sides; a
    table touching conv_new's FIRST/LAST line can glue to the surviving
    prefix/suffix — there alone the splice edge must sit at a line boundary:
    an append (mid-line splice, table behind a blank line inside new_string)
    converts, a mid-line edit_document replacement never does.
    """
    from table_serialize import _TABLE_ANCHOR, _row_has_pipe

    edge_clean, prev_before, after_first = _splice_edge_state(
        content, mask, from_cp, to_cp,
    )
    if not edge_clean:
        return False
    from_cp_line_start = from_cp == 0 or content[from_cp - 1] == "\n"
    to_cp_line_end = to_cp >= len(content) or content[to_cp] == "\n"

    lines = conv_new.split("\n")
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if not _TABLE_ANCHOR.fullmatch(line.strip()):
            continue
        if i > 0:
            prev = lines[i - 1]
        else:
            if not from_cp_line_start and prev_before is not None:
                return False  # the anchor would glue onto the prefix's last line
            prev = prev_before
        if i < last:
            nxt = lines[i + 1]
        else:
            if not to_cp_line_end and after_first is not None:
                return False  # the anchor would glue onto the suffix's first line
            nxt = after_first
        if prev is not None and _row_has_pipe(prev):
            return False
        if nxt is not None and _row_has_pipe(nxt):
            return False
    return True


def _convert_resolved_edit_tables(
    resolved: list[dict], current_content: str,
) -> tuple[dict[str, list[list[str]]] | None, list[dict]]:
    """GFM tables → objects for agent-authored EDITS, staged per resolved edit:
    convert the new_text alone, gate the result on the post-splice skip rule,
    and only then bound the cells (the authored-cell 400 fires for edits that
    WOULD convert — a fenced or partial splice of the same body stays text).
    Accepted edits get their ``new_text`` rewritten to anchor form; the fresh
    models are returned for ``route_document_edits(new_tables=…)`` (the anchor
    splices and the model builds land in ONE transaction there) plus the
    ``tables`` result field. Callers run this BEFORE the checkpoint, so a
    refusal writes nothing.
    """
    from markdown_normalize import fence_mask

    from table_serialize import extract_table_labels, import_gfm_tables

    mask = fence_mask(current_content.split("\n"))
    new_tables: dict[str, list[list[str]]] = {}
    tables_out: list[dict] = []
    for r in resolved:
        conv_new, tables_json = import_gfm_tables(r["new_text"])
        if tables_json == "{}":
            continue  # nothing tabular authored in this splice
        if not _anchors_own_their_lines(
            conv_new, current_content, mask, r["from_cp"], r["to_cp"],
        ):
            continue
        from agent.table_writes import reject_table_overflow

        models = json.loads(tables_json)
        reject_table_overflow([m["rows"] for m in models.values()])
        r["new_text"] = conv_new
        for tid, model in models.items():
            new_tables[tid] = model["rows"]
        tables_out.extend(
            {"table_id": tid, "label": label}
            for tid, label in extract_table_labels(conv_new).items()
        )
    return (new_tables or None), tables_out


async def _route_descending_batch(
    doc_id: str, project_id: str, resolved: list[dict], on_commit, new_tables,
) -> None:
    """Apply in DESCENDING from_cp order so each surgical splice's earlier
    offsets stay valid (earlier text unshifted) under the ONE _write_lock →
    ONE Y.Doc update → ONE publish (route_document_edits, audit F5). A failure
    AFTER the mark-first seam (on_commit ran) surfaces AppliedUnverifiedError —
    the row IS applied even though the write failed (reload shows applied);
    without a mark the original error propagates."""
    try:
        await doc_state.route_document_edits(
            doc_id=doc_id, project_id=project_id,
            edits=[{"from_cp": r["from_cp"], "to_cp": r["to_cp"],
                    "new_text": r["new_text"], "original_text": r["original_text"]}
                   for r in sorted(resolved, key=lambda r: r["from_cp"], reverse=True)],
            new_tables=new_tables,
        )
    except AppliedUnverifiedError:
        raise
    except Exception:
        if on_commit is not None:
            logger.exception("Content mutation failed after mark-first for %s", doc_id)
            raise AppliedUnverifiedError()
        raise


async def _apply_resolved_edits(
    doc_id: str, project_id: str, current_content: str, current_tables_json,
    resolved: list[dict], skipped: list[dict], on_commit,
    convert_tables: bool = False,
) -> dict:
    """The apply pass: ONE checkpoint (applied >= 1), the mark-first seam, then
    descending splices. Returns the caller's result dict; maps a post-mark write
    failure to AppliedUnverifiedError (see INVARIANT(data-loss) on the core)."""
    # GFM tables → objects for each edit's AUTHORED new_text that survives the
    # post-splice skip rule — BEFORE the checkpoint so the authored-cell cap
    # (400) leaves nothing written. The converted anchors and models reach
    # route_document_edits together: ONE transaction, ONE publish.
    new_tables = None
    tables_out: list[dict] = []
    if convert_tables:
        new_tables, tables_out = _convert_resolved_edit_tables(
            resolved, current_content,
        )
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
    await _route_descending_batch(doc_id, project_id, resolved, on_commit, new_tables)
    result = {
        "applied": len(resolved), "skipped": skipped, "checkpoint": True,
        "applied_ranges": [r["from_cp"] for r in resolved],
    }
    if tables_out:
        result["tables"] = tables_out
    return result


async def _resolve_validate_apply_edits(
    *,
    doc_id: str,
    edits: list[dict],
    project_id: str,
    on_commit=None,
    region_check=None,
    convert_tables: bool = False,
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
    `convert_tables` enables the edit-path GFM→object conversion (markdown
    bodies only — the caller computes `not is_artifact`); each edit converts
    under the post-splice skip rule (see `_convert_resolved_edit_tables`).
    Returns {applied, skipped, checkpoint, tables?, noop?}. Raises HTTPException
    (stale/miss/overlap/full_rewrite/cell-too-large) or
    RegionContainmentReject; when on_commit ran and the write then fails,
    re-raises AppliedUnverifiedError (mark-first: applied)."""
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
        resolved, skipped, on_commit, convert_tables=convert_tables,
    )
