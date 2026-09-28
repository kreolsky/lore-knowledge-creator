"""Shared publishability guard for the public-share surface.

# SYSTEM: share-guard — the "is this document publishable" ancestry check.

The agent-config subtree (system root + descendants) is UNPUBLISHABLE: it is the
agent's operating state (prompts, personas, rules, skills, memory), not a
document authored for readers. This guard is enforced at BOTH the mint
chokepoint (routes/document_shares.py::create_share) AND the resolve funnel
(routes/public_share.py::resolve_share) — a row minted before the guard is still
caught at resolve. See the INVARIANT(security, ancestry) in public_share.py.

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

from db import get_ancestor_ids


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
