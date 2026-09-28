"""get_ancestor_ids — contract tests for the parent-chain walk.

# WHY: Fix 2 of the perf audit replaces the full-project-scan strategy with an
# iterative parent-key walk (O(depth) instead of O(project size)). These tests pin
# the result contract so the strategy swap cannot change behavior. See
# plans/imperative-knitting-curry.md (Fix 2).
"""

import uuid

import pytest

from db import create_record, get_ancestor_ids, soft_delete


async def _doc(pid: str, parent_id: str | None = None) -> str:
    did = str(uuid.uuid4())
    data = {"project_id": pid, "title": f"d-{did[:6]}", "path": f"/{did}"}
    if parent_id is not None:
        data["parent_id"] = parent_id
    await create_record("documents", did, data)
    return did


@pytest.fixture
async def chain(project_with_doc):
    """A 3-level chain root → mid → leaf in a fresh project."""
    pid, _, _ = project_with_doc
    root = await _doc(pid)
    mid = await _doc(pid, root)
    leaf = await _doc(pid, mid)
    return pid, root, mid, leaf


@pytest.mark.asyncio
async def test_returns_chain_including_self(chain):
    pid, root, mid, leaf = chain
    assert await get_ancestor_ids(leaf, pid) == [leaf, mid, root]


@pytest.mark.asyncio
async def test_root_returns_only_self(chain):
    pid, root, _, _ = chain
    assert await get_ancestor_ids(root, pid) == [root]


@pytest.mark.asyncio
async def test_stops_at_soft_deleted_ancestor(chain):
    pid, root, mid, leaf = chain
    await soft_delete("documents", mid)
    # The chain breaks at the deleted mid; leaf alone remains reachable.
    assert await get_ancestor_ids(leaf, pid) == [leaf]


@pytest.mark.asyncio
async def test_project_scoped_matches_unscoped(chain):
    """Passing project_id must yield the identical result to the unscoped walk."""
    pid, root, mid, leaf = chain
    for did in (leaf, mid, root):
        assert await get_ancestor_ids(did, pid) == await get_ancestor_ids(did, None)
