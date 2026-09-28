"""Subtree scope helpers — the agent-key sandbox wall (plan
"subtree-scoped-agent-keys").

# SYSTEM: scope — subtree-membership + enumeration for scoped agent keys.

A scoped agent key carries a `scope_root` (= api_keys.document_id): the key is
sandboxed to that document's subtree (root + all descendants). An empty/None
scope_root means whole-project (today's behavior — legacy keys). Scope is a
CEILING on top of RBAC (`get_document_access`); it can only narrow, never widen.

# ARCH: membership is re-walked per call via the existing,
perf-audited `db.get_ancestor_ids` (O(depth), depth-capped 50), and enumeration
uses `db.get_descendant_ids` (one project scan + in-memory BFS). No new
materialization infra. The wall is "one bounded walk per call", not a cached
closure table.

# WHY: scope ⊂ RBAC. Every shared executor calls `require_doc_in_scope`
# AFTER its `get_document_access` gate — scope stacks on top, never replaces it.
# Why: an agent must never exceed the owning user's RBAC; scope only narrows the
# visible volume inside the project, it cannot widen access.
"""
from __future__ import annotations

from fastapi import HTTPException

from db import get_ancestor_ids, get_descendant_ids


def out_of_scope_detail(scope_root: str, *, empty_intersection: bool = False) -> str:
    """The single producer of the out-of-scope 403 details.

    Names the scope root and the recovery (list the in-scope ids), so a weak model
    that reads only the 403 payload can re-target an in-scope document instead of
    concluding the whole surface is unusable. The detail is dynamic (it carries the
    root id), so this helper — not a bare module-level string — is the ONE source:
    `require_doc_in_scope` raises it, the init SCOPE section renders it, and
    readonly_executors returns it via `exc.detail`. The `empty_intersection`
    variant (the search narrow that intersected to nothing — search_exec used to
    hand-write a near-copy here) is produced by the SAME helper so the recovery
    tail can never drift between the two.

    # INVARIANT (soft-error mapping): the text MUST NOT contain 'not found' or
    # 'not in this project' — dispatch._raise_for_soft_error maps a soft error to 404
    # only when the text carries either phrase, so a scope 403 that quoted them would
    # be mis-mapped to a uniform 404 and lose the recovery signal. test_read_document
    # _out_of_scope_is_403_naming_root pins that the scope path stays a 403.
    """
    recovery = (
        f"get_project_structure(start_id={scope_root}) to list the in-scope ids."
    )
    if empty_intersection:
        return (
            "The requested subtree holds no documents inside this agent "
            f"key's subtree scope (root {scope_root}). Retry with an "
            f"under_document_id inside that scope — call {recovery}"
        )
    return (
        f"Document is outside this agent key's subtree scope (root {scope_root}). "
        f"Retry against a document_id inside that subtree — call {recovery}"
    )


async def in_subtree(scope_root: str | None, doc_id: str) -> bool:
    """True if `doc_id` lies inside the subtree rooted at `scope_root`.

    Empty/None scope_root ⇒ whole project ⇒ always True (legacy behavior).
    Membership is decided by walking `doc_id`'s ancestor chain and checking
    whether `scope_root` appears in it (the root is its own ancestor, so a doc
    IS in its own subtree).
    """
    if not scope_root:
        return True
    if not doc_id:
        return False
    return scope_root in await get_ancestor_ids(doc_id)


async def require_doc_in_scope(scope_root: str | None, doc_id: str) -> None:
    """Raise HTTPException(403) if `doc_id` is outside the subtree.

    # INVARIANT(security): called AFTER get_document_access in every shared
    # executor — scope is a ceiling on top of RBAC (agent must fail BOTH; scope ⊂ RBAC).
    # Why: a DISTINCT out-of-scope 403 (not a uniform 404) signals the wall — the doc
    # may exist and be RBAC-visible to the owning user, but is outside this key's sandbox.
    # Why (kept by user decision): the distinct 403 IS a narrow
    # existence oracle (a cooperative agent can tell "exists but out of scope" from
    # "does not exist"), and that trade-off was consciously accepted — an
    # informative 403 that stops a cooperative agent from blind-retrying is worth
    # more here than uniform-404 opacity, because the agent already acts under the
    # owning user's identity (it can enumerate in-scope, and RBAC still gates every
    # read). Do NOT "fix" this to a uniform 404 without revisiting that decision.
    """
    if not await in_subtree(scope_root, doc_id):
        raise HTTPException(
            status_code=403,
            detail=out_of_scope_detail(scope_root or ""),
        )


def _reject_memory_target(target: dict) -> None:
    """Refuse a content mutation aimed at a project-memory fact document.

    # WHY: no agent content mutation may target an `is_memory` document — its
    # body IS the fact, written only by the consolidation apply path (the one door,
    # D9). Why: prose written outside apply carries no provenance (the reference thread
    # that makes the knowledge base rebuildable, D8), and the apply path is the only
    # route that stamps it. The apply path writes fact content through
    # `route_document_content`, which deliberately does NOT pass through this gate.

    Called from `gate_mutation_target` rather than from `edit_document`, because the
    class is any scoped agent content mutation: edit_document, append_to_document and
    edit_table_cell all funnel through that one gate, and covering a single member
    leaves the others as the same hole under a different tool name.
    """
    if target.get("is_memory"):
        raise HTTPException(
            status_code=403,
            detail=(
                "This is a project memory fact — its body is the fact itself, written "
                "only by the consolidation apply path. Use consolidate_memory to change "
                "what it says."
            ),
        )


async def _target_not_found_404():
    """Raise the mutating target-404. Shared by the missing/deleted and the
    cross-project branch so the two details can never drift."""
    raise HTTPException(status_code=404, detail="Target document not found")


async def gate_mutation_target(
    *, user: dict, doc_id: str, project_id: str, scope_root: str | None,
) -> tuple[dict, str]:
    """Fetch + RBAC + scope gate for a scoped mutation target; returns (doc row, access).

    The identical guard sequence at every scoped mutation entry point (edit_document /
    append / edit_table_cell, auto + confirm-apply-direct): fetch → 404 on
    missing/deleted → 404 on cross-project (uniform, no existence oracle) → 403 unless
    per-doc access is 'full' → subtree wall. Extracted to kill the ~6-way duplication
    whose drift risk caused the historical "create_reference apply 400" scope bug.
    Returns the resolved `access` too (always 'full' on the success path) because the
    confirm routes thread it into _resolve_apply_or_force.

    # INVARIANT(security): scope ⊂ RBAC — require_doc_in_scope runs AFTER the full-
    # access gate, never instead of it. Why: a scoped key must never exceed the owning
    # user's RBAC; the wall only narrows the volume inside the project.

    NOTE: the confirm-path _resolve_edit_target (apply_executors) deliberately does NOT
    use this — it is session-gated (no scope_root wall) and returns 403 (not 404) on a
    cross-project doc_id. That divergence is intentional; do not fold it in here.

    This gate is the SINGLE 404 funnel for every scoped-mutation target
    (edit_document / append_to_document / edit_table_cell / table tools, auto +
    confirm) — the shared detail lives in _target_not_found_404, not per call
    site.
    """
    from access import get_document_access
    from db import fetch_one

    target = await fetch_one("documents", doc_id)
    if not target or target.get("deleted_at"):
        await _target_not_found_404()
    if target.get("project_id") != project_id:
        # Uniform 404 for missing AND cross-project (no existence oracle).
        await _target_not_found_404()
    access = await get_document_access(doc_id, user)
    if access != "full":
        raise HTTPException(status_code=403, detail="Full access required to edit")
    _reject_memory_target(target)
    await require_doc_in_scope(scope_root, doc_id)
    return target, access


async def subtree_doc_ids(scope_root: str | None, project_id: str) -> list[str]:
    """Return the subtree's doc ids ([root] + descendants), or [] when unscoped.

    Empty/None scope_root ⇒ whole project ⇒ returns [] (the sentinel the
    enumeration tools use to mean "no subtree restriction — do not filter").
    A scoped key always returns at least [scope_root] (the root itself), so []
    unambiguously means "whole project".
    """
    if not scope_root:
        return []
    return [scope_root] + await get_descendant_ids(scope_root, project_id)


def resolve_scoped_parent(scope_root: str | None, parent_id: str | None) -> str | None:
    """Resolve the effective parent for an agent create/move under scope.

    # ARCH: when the key is scoped and the agent
    # omits parent_id, the node must land INSIDE the wall — default to
    # scope_root, not the project root. Otherwise a scoped agent could create a
    # top-level doc outside its sandbox, or MOVE one out through the same null
    # (move resolves its destination here BEFORE validating it, so the wall's
    # require_doc_in_scope runs on the real scope root instead of being skipped).
    # Unscoped keys keep today's behavior (parent_id as-passed, None ⇒ project
    # root).
    """
    if not scope_root:
        return parent_id
    return parent_id or scope_root


__all__ = [
    "in_subtree",
    "require_doc_in_scope",
    "out_of_scope_detail",
    "subtree_doc_ids",
    "resolve_scoped_parent",
]
