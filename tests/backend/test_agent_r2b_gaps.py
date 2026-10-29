"""R2b test-gap closures: structure subtree depth/IDOR, _warn_if_sidecars_outweigh_slice,
route_tables_mutation no-session.

The structure pair moved onto the plan structure-layered-walk shape (one
project-filtered topology query + pure walk) but pins the SAME invariants the
old _bfs_subtree tests pinned: the depth cap and the cross-project wall.
"""


import pytest

# ─── structure subtree walk: depth cap + cross-project wall (IDOR) ───────────


async def _seed_doc(db, doc_id, pid, parent=None, title=None):
    await db.query(
        "CREATE type::record('documents', $id) SET project_id = $pid, "
        "parent_id = $parent, title = $title, content = '', path = $path, "
        "is_index = false, is_reference = false, deleted_at = NONE",
        {"id": doc_id, "pid": pid, "parent": parent,
         "title": title or doc_id, "path": f"{doc_id}.md"},
    )


@pytest.mark.asyncio
async def test_topology_cross_project_rows_leak_nothing(test_db):
    """IDOR wall: the ONE topology SELECT is project-filtered, so a doc whose
    PARENT lives in another project must not pull that project's subtree into
    ours."""
    from agent.structure_exec import (
        _children_map,
        _load_topology,
        _subtree_visible,
    )

    p1, p2 = "r2b-bfs-a", "r2b-bfs-b"
    await test_db.query(
        "DELETE documents WHERE project_id IN $pids", {"pids": [p1, p2]},
    )
    await _seed_doc(test_db, "r2b-x-root", p1)
    # foreign doc in p2 whose parent is OUR root — p1's map must never contain
    # it (the walk can only emit ids from the project-filtered topology).
    await _seed_doc(test_db, "r2b-foreign", p2, parent="r2b-x-root")

    topo = await _load_topology(test_db, p1)
    children = _children_map(topo)
    assert "r2b-foreign" not in topo, "cross-project row leaked into the topology"
    ids = _subtree_visible(topo, children, "r2b-x-root", None)
    assert ids == ["r2b-x-root"]
