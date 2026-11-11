"""Shared publishability guard for the public-share surface.

# SYSTEM: share-guard — the "is this document publishable" check: the agent-config subtree by ancestry + the project index by role.

Two unpublishable classes, one guard:
  - the agent-config subtree (system root + descendants), by ANCESTRY —
  - the project's index doc, by role.

The agent-config subtree is the agent's operating state (prompts, personas,
rules, skills, memory), not a document authored for readers. The index doc
(plan "project-index-as-project-face") is the project's FACE: a share on it
reads as "an anonymous link on the whole project", which is a non-goal —
project-level sharing lives in the project settings. The guard is enforced at
BOTH the mint chokepoint (routes/document_shares.py::create_share) AND the
resolve funnel (routes/public_share.py::resolve_share) — a row minted before
the guard is still caught at resolve. See the INVARIANT(security, ancestry) in
public_share.py.

# ARCH: guarded by ANCESTRY, not the is_system flag. Why: only the skeleton
# (system root + the role folders) carries is_system=true; everything a user
# actually writes inside the subtree (a persona, a skill, a memory note) is
# is_system=false (agent_config.py: "Children are plain docs"). A flag-only
# check would publish those leaves.
#
# The system root id is DETERMINISTIC (agent_config_seed._deterministic_id →
# "sys-system_root-<project_id>") and has parent_id=None, so no document sits
# above it: a subtree share can never cover it, and the per-request ancestry
# walk fully closes the read path. No doc_ids subtraction is needed.
"""
from __future__ import annotations

from db import fetch_one, get_ancestor_ids


def system_root_id(project_id: str) -> str:
    """Deterministic system_root document id for a project.

    Pure string (no DB query): mirrors agent_config_seed._deterministic_id so the
    guard locates the SAME root the seed writes. If the root was never seeded
    (project never ran an agent turn) this id simply matches no real doc and no
    parent chain, so the guard correctly returns "not under system root".
    """
    return f"sys-system_root-{project_id}"


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
    return sys_root in await get_ancestor_ids(document_id, project_id)


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
