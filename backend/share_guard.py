"""Shared publishability guard for the public-share surface.

# SYSTEM: share-guard — the "is this document publishable" check: the agent-config subtree by ancestry + the project index by role; also hosts the agent-write refusal under the system root.

Two unpublishable classes, one guard:
  - the agent-config subtree (system root + descendants), by ANCESTRY —
  - the project's index doc, by role.

The agent-config subtree is the agent's operating state (prompts, personas,
rules, skills, memory), not a document authored for readers. The index doc
is the project's FACE: a share on it
reads as "an anonymous link on the whole project", which is a non-goal —
project-level sharing lives in the project settings. The module also hosts the
agent-write refusal under the system root (the same ancestry walk, applied to
agent tool writes). The guard is enforced at
BOTH the mint chokepoint (routes/document_shares.py::create_share) AND the
resolve funnel (routes/public_share.py::resolve_share) — a row minted before
the guard is still caught at resolve. See the INVARIANT(security, ancestry) in
public_share.py.

# ARCH: guarded by ANCESTRY, not the is_system flag. Why: only the skeleton
# (system root + the role folders) carries is_system=true; everything a user
# actually writes inside the subtree (a persona, a skill, a memory note) is
# is_system=false (agent_config.py: children are plain docs). A flag-only
# check would publish those leaves.
#
# The system root id is DETERMINISTIC (agent_config_seed._deterministic_id →
# "sys-system_root-<project_id>") and has parent_id=None, so no document sits
# above it: a subtree share can never cover it, and the per-request ancestry
# walk fully closes the read path. No doc_ids subtraction is needed.
"""
from __future__ import annotations

from fastapi import HTTPException

from db import fetch_one, get_ancestor_ids


def system_root_id(project_id: str) -> str:
    """Deterministic system_root document id for a project.

    Pure string (no DB query): mirrors agent_config_seed._deterministic_id so the
    guard locates the SAME root the seed writes. If the root was never seeded
    (project never ran an agent turn) this id simply matches no real doc and no
    parent chain, so the guard correctly returns "not under system root".
    """
    return f"sys-system_root-{project_id}"


def memory_root_id(project_id: str) -> str:
    """Deterministic Memory folder document id — the shape of
    agent_config_seed.memory_folder_id, built here because agent_config_seed
    drags documents.service into this leaf."""
    # WHY: restates agent_config_seed.memory_folder_id's literal; importing
    # agent_config_seed here would drag documents.service into this leaf.
    return f"sys-memory_folder-{project_id}"


async def is_unshareable_by_ancestry(document_id: str, project_id: str) -> bool:
    """True if the doc IS the system root or descends from it.

    ANCESTRY, not the is_system flag: a is_system=false leaf written under the
    system root (a persona, a skill, a memory note) is still caught — the flag
    marks only the skeleton.
    """
    sys_root = system_root_id(project_id)
    if document_id == sys_root:
        return True
    # get_ancestor_ids is nearest-first and includes document_id at [0]; O(depth).
    # WHY through_deleted: a live doc under a trashed folder inside the subtree
    # is still inside it — stopping at the trashed folder would call it outside.
    return sys_root in await get_ancestor_ids(document_id, project_id, through_deleted=True)


async def refuse_under_system_root(doc_id: str, project_id: str, tool_name: str) -> None:
    """403 when `doc_id` is the system root or under it (Memory included) — for
    the agent paths that never confirm (imports, generated references, MCP file
    attach/reprocess).

    # INVARIANT(security): an unconfirmable write never lands under the system root.
    # Why: everything there except Memory is injected into the agent's prompt
    # (references too — the loader does not filter them), and Memory has one
    # door for facts; an auto-only import would bypass the confirm every other
    # write there gets.
    """
    if await is_unshareable_by_ancestry(doc_id, project_id):
        raise HTTPException(
            status_code=403,
            detail=f"{tool_name} cannot write into the agent configuration folder",
        )


async def is_agent_instruction_doc(document_id: str, project_id: str) -> bool:
    """True if the doc is part of the agent's instructions: the system root or a
    descendant, EXCEPT the Memory subtree (facts are data with their own apply
    path). Ancestry, not the is_system flag — see INVARIANT(security) in
    agent/apply_policy.py.
    """
    sys_root = system_root_id(project_id)
    if document_id == sys_root:
        return True
    ancestors = await get_ancestor_ids(document_id, project_id, through_deleted=True)
    return sys_root in ancestors and memory_root_id(project_id) not in ancestors


async def is_unshareable(
    document_id: str, project_id: str, *, doc: dict | None = None,
) -> bool:
    """True if the doc must never be published: the agent-config subtree (by
    ancestry) or the project's index doc (the project's face).

    # INVARIANT(security): the index doc is unpublishable in BOTH funnels —
    # mint (create_share → 400) and resolve (uniform 404) — from this one
    # guard. Why: shares on the index exist today (users shared it trying to
    # share the project), and the index scope now covers the whole project, so
    # an existing subtree share on it would resolve to an anonymous link on
    # everything. Existing rows are NOT deleted — resolve 404s them (the
    # system-subtree precedent). `doc` lets a caller that already holds the
    # row (resolve_share fetches it first) skip the re-fetch.
    """
    if await is_unshareable_by_ancestry(document_id, project_id):
        return True
    row = doc if doc is not None else await fetch_one("documents", document_id)
    return bool(row and row.get("is_index"))
