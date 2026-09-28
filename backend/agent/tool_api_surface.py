"""Standalone batch edit apply for agent writes (Tool-API / direct path).

# (see SYSTEM: chat-agent-mode, agent/__init__.py).

# ARCH: the Tool-API (routes.tool_api) and the in-editor agent both build on these.
# They are PUBLIC so the boundary test (no private chat imports outside the package)
# is respected.

# INVARIANT: identical convergence path to the in-editor agent. There is exactly
# ONE source of truth for content mutation; this must never bypass collab.  Why: a write that bypasses collab diverges from the live Y.Doc and forfeits OT convergence — every mutation path (API/tool surface or in-editor agent) must funnel through the same live-session apply.

# The write cluster lives in focused modules (no re-exports — import the owner):
#   - doc_state            : resolve_live_doc_state, reject_remote_images,
#                            route_document_content / route_document_edits,
#                            finalize_content_mutation (the leaf)
#   - collab_writes        : create_document/reference_via_collab, checkpoint, presence
#   - apply_edits_resolver : _resolve_validate_apply_edits + RegionContainmentReject
#   - table_writes         : validate/route/apply table-cell edits
# This module keeps the batch apply entry (apply_edits_to_document /
# apply_edit_to_document) and the resolve_edit_range gate wrapper.
"""
import logging

from agent.apply_edits_resolver import (
    RegionContainmentReject,
    _resolve_validate_apply_edits,
    region_containment_error,
)
from agent.collab_writes import broadcast_agent_presence
from config import AGENT_EDIT_MAX_CHARS
from models import RegionRef

logger = logging.getLogger(__name__)


def resolve_edit_range_for_gate(
    content: str, old_string: str, *, full_rewrite_fraction: float,
) -> tuple[int, int, bool] | str:
    """Public wrapper around `resolve_edit_range` for cross-package pre-validation gates.

    # WHY: `resolve_edit_range` is private to the agent package (module-boundary test
    # forbids importing private routes.chat names from outside it); this named export
    # is the sanctioned entry point for callers like routes.tool_api's confirm-mode gate.
    """
    from agent.edit_primitives import resolve_edit_range

    return resolve_edit_range(
        content, old_string, full_rewrite_fraction=full_rewrite_fraction,
    )


async def apply_edits_to_document(
    *,
    doc_id: str,
    edits: list[dict],
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    region: RegionRef | None = None,
) -> dict:
    """Apply a batch of str_replace edits directly through the live CRDT path + ONE
    checkpoint.

    Each edit dict: {old_string, new_string}. All edits resolve against the SAME
    original snapshot; ranges must not overlap (independent regional edits). Applied
    atomically: all-or-nothing (one failing edit rejects the whole batch with its
    index), one pre-edit checkpoint, one Y.Doc publish.

    Idempotent already-applied skip: an edit whose old_string is gone AND
    whose new_string is present exactly once (same folded projection) is dropped and
    reported in `skipped`. A new_string that is absent or ambiguous still raises the
    enriched 409 — a wrong old_string is surfaced, never silently swallowed.

    # WHY: identical convergence path to the in-editor agent (route_document_edits
    # is the one surgical convergence path). Exactly ONE source of truth for
    # content mutation; this must never bypass collab.  Why: the batch path shares the in-editor agent's single mutation source — bypassing collab here would diverge from the live Y.Doc and forfeit OT convergence.
    # WHY (subtree-scoped agent keys): `require_doc_in_scope` runs AFTER the
    # access gate — scope is a ceiling on top of RBAC, never a replacement.
    # WHY: full-rewrite ban is per-edit (each edit in the batch is still
    # individually small/unique/non-full-rewrite via resolve_edit_range); batching
    # pointwise edits is exactly the sanctioned path, not a rewrite.  Why: the full-rewrite ban is enforced per-edit so a batch can't smuggle a wholesale replacement via aggregation — many small pointwise edits is the sanctioned path.

    Raises HTTPException on RBAC/not-found/stale/full-rewrite/oversize/out-of-scope/
    overlap. The error detail carries the failing edit's `index` so the model can
    fix just that one.

    Returns:
      {status:"applied", doc_id, applied:<n>, skipped:[{index, at_cp, reason}], checkpoint:<bool>}
      All-skipped → {status:"applied", noop:true, doc_id, applied:0, skipped:[…], checkpoint:false}
    """
    from fastapi import HTTPException

    if not edits:
        raise HTTPException(status_code=400, detail="No edits supplied")

    # M7: shared fetch+RBAC+scope gate (was inlined here + at the tool_api routes).
    from scope import gate_mutation_target

    await gate_mutation_target(
        user=user, doc_id=doc_id, project_id=project_id, scope_root=scope_root,
    )

    # Per-edit oversize guard (before any lock — mirrors the single-edit cap).
    for e in edits:
        if isinstance(e.get("new_string"), str) and len(e["new_string"]) > AGENT_EDIT_MAX_CHARS:
            raise HTTPException(status_code=413, detail="Replacement text too large")

    from markdown_normalize import normalize_list_spacing, unwrap_placeholder_brackets

    # Normalize each edit's new_string spacing once (mirrors the single-edit normalize),
    # then unwrap `(<id>)` placeholder-bracket destinations — one of the three agent
    # write sites for the unwrap (batch edit executor; append converges onto it).
    normalized = [
        {
            "old_string": e["old_string"],
            "new_string": unwrap_placeholder_brackets(normalize_list_spacing(e["new_string"])),
        }
        for e in edits
    ]

    from doc_edit_lock import edit_lock

    # Pinned-region containment (direct apply path): a pinned session's driver
    # forwards the resolved region on the call body; the core re-resolves each edit
    # against live content and rejects any range escaping the fragment. The gate is
    # the relocated region_containment_error — the same hard check the deleted
    # proposal apply path used, now on the direct path.
    region_check = None
    if region is not None:
        def _region_check(from_cp: int, to_cp: int) -> str | None:
            return region_containment_error(
                edit_from=from_cp, edit_to=to_cp, edit_doc_id=doc_id,
                has_region=True, region=region,
            )

        region_check = _region_check

    # ARCH: per-doc Redis lock around the FULL resolve→splice→checkpoint→write.
    # See apply_edit_to_document (single-edit) for the lost-update rationale: the
    # validation pass reads the live state, then the checkpoint + batch write must
    # be serialized against concurrent same-doc edits — on any replica. Presence
    # broadcast stays OUTSIDE (best-effort may do network I/O).
    async with edit_lock(doc_id):
        try:
            result = await _resolve_validate_apply_edits(
                doc_id=doc_id, edits=normalized, project_id=project_id,
                region_check=region_check,
            )
        except RegionContainmentReject as exc:
            # A pinned-region edit escaping the fragment is rejected hard — 409
            # with the region_locked code (the deleted proposal path's
            # region_out_of_scope status is gone; the wire signal is region_locked).
            raise HTTPException(
                status_code=409,
                detail={"code": "region_locked", "detail": exc.detail},
            )
    # Best-effort presence: tell live editors the agent edited this doc.
    await broadcast_agent_presence(doc_id, user.get("user_id"))
    return {"status": "applied", "doc_id": doc_id, **result}


async def apply_edit_to_document(
    *,
    doc_id: str,
    old_string: str,
    new_text: str,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    region: RegionRef | None = None,
) -> dict:
    """Back-compat wrapper: a single edit is a batch of one (plan
    "quizzical-mixing-marble"). Coalesces the legacy singular form to a one-element
    edits list and delegates to apply_edits_to_document. Existing single-edit
    callers (proposal apply, Tool-API direct) keep their signature unchanged."""
    return await apply_edits_to_document(
        doc_id=doc_id,
        edits=[{"old_string": old_string, "new_string": new_text}],
        project_id=project_id, user=user, scope_root=scope_root, region=region,
    )
