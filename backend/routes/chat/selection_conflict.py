"""Advisory selection-conflict pre-apply check.

# INVARIANT: selection-conflict NEVER overrides CRDT convergence.
# Why: it is a best-effort, NON-atomic pre-apply UX check — if an agent edit's
# resolved region intersects another participant's active selection, reject with a
# message rather than silently merging across a cursor. The authoritative last
# line stays the CRDT re-resolve in `_apply_edit_proposal` /
# `apply_edit_to_document` → `merge_live_content`. This module does NOT mutate
# anything and is NOT integrated into the atomic apply path — only the apply
# CALLERS read it as a pre-check.
#
# Why a SEPARATE resolve: this check re-resolves `old_string` against live
# content independently of the apply path. By design the two reads race (content
# may shift between the check and the splice); on any race the CRDT re-resolve
# in the apply path wins. A second atomic conflict mechanism in one path
# produces subtle bugs — there is exactly one source of truth (the CRDT),
# selection-conflict only improves UX.
"""
from __future__ import annotations

import logging

import settings
from agent.edit_primitives import resolve_edit_range
from collab import selection_registry as reg
from collab.events import merge_live_content

from db import fetch_one

logger = logging.getLogger(__name__)


async def selection_conflict_check(
    doc_id: str,
    old_string: str,
    exclude_user_id: str | None,
    *,
    content: str | None = None,
) -> dict | None:
    """Return a conflicting participant dict if `old_string`'s resolved region on
    `doc_id` intersects another participant's active selection; else None.

    Reads live content (via merge_live_content) unless `content` is supplied
    (test/known-content path). An unresolvable old_string (not_found / ambiguous /
    full_rewrite) returns None — the apply will itself reject it and region-lock
    must not preempt that path. Never mutates.

    Returns reg.conflicting()'s dict: {user_id, user_name, from_cp, to_cp}.
    """
    if content is None:
        target = await fetch_one("documents", doc_id)
        if not target or target.get("deleted_at"):
            return None
        content = merge_live_content(target, doc_id).get("content") or ""

    rng = resolve_edit_range(
        content, old_string,
        full_rewrite_fraction=await settings.get("AGENT_FULL_REWRITE_FRACTION"),
    )
    if isinstance(rng, str):
        return None
    from_cp, to_cp, _folded = rng
    return reg.conflicting(doc_id, from_cp, to_cp, exclude_user_id=exclude_user_id)


def selection_conflict_response(conflict: dict) -> dict:
    """Build the structured rejection payload surfaced to the user/model.

    # ARCH: a region-lock conflict is an EXPLICIT
    rejection with the conflicting participant's name — never a silent skip.
    """
    return {
        "code": "region_locked",
        "user": conflict.get("user_name") or conflict.get("user_id"),
        "detail": (
            f"Edit region overlaps an active selection by "
            f"{conflict.get('user_name') or conflict.get('user_id')} — "
            f"wait for them to finish, then retry."
        ),
    }
