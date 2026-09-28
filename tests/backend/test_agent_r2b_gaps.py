"""R2b test-gap closures: structure subtree depth/IDOR, _warn_if_sidecars_outweigh_slice,
route_tables_mutation no-session.

The structure pair moved onto the plan structure-layered-walk shape (one
project-filtered topology query + pure walk) but pins the SAME invariants the
old _bfs_subtree tests pinned: the depth cap and the cross-project wall.
"""

import logging

import pycrdt as Y
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
async def test_subtree_walk_depth_cap(test_db):
    from agent.structure_exec import (
        _children_map,
        _load_topology,
        _subtree_visible,
    )

    pid = "r2b-bfs-p1"
    await test_db.query("DELETE documents WHERE project_id = $pid", {"pid": pid})
    # chain: root -> c1 -> c2 -> c3
    await _seed_doc(test_db, "r2b-root", pid)
    await _seed_doc(test_db, "r2b-c1", pid, parent="r2b-root")
    await _seed_doc(test_db, "r2b-c2", pid, parent="r2b-c1")
    await _seed_doc(test_db, "r2b-c3", pid, parent="r2b-c2")

    topo = await _load_topology(test_db, pid)
    children = _children_map(topo)

    full = _subtree_visible(topo, children, "r2b-root", None)
    assert full == ["r2b-root", "r2b-c1", "r2b-c2", "r2b-c3"]

    one_level = _subtree_visible(topo, children, "r2b-root", 1)
    assert one_level == ["r2b-root", "r2b-c1"]

    two_levels = _subtree_visible(topo, children, "r2b-root", 2)
    assert two_levels == ["r2b-root", "r2b-c1", "r2b-c2"]


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


# ─── _warn_if_sidecars_outweigh_slice tripwire ───────────────────────────────


def test_warn_if_sidecars_outweigh_slice_fires_with_numbers(caplog):
    from agent.readonly_executors import _warn_if_sidecars_outweigh_slice

    result = {
        "tables": [{"id": "t1", "rows": [["x" * 500]]}],
        "references": [{"id": f"r{i}", "title": "t"} for i in range(20)],
    }
    with caplog.at_level(logging.WARNING, logger="agent.readonly_executors"):
        _warn_if_sidecars_outweigh_slice("doc-1", result, "tiny slice")
    assert any("doc-1" in r.message and "sidecar" in r.message for r in caplog.records), (
        [r.message for r in caplog.records]
    )
    # WHY the numbers ride the message: the formatter drops `extra` — a tripwire
    # without measurements says nothing.
    fired = next(r for r in caplog.records if "sidecar" in r.message)
    assert any(ch.isdigit() for ch in fired.message)


def test_warn_if_sidecars_outweigh_slice_quiet_when_light():

    from agent.readonly_executors import _warn_if_sidecars_outweigh_slice

    result = {"tables": [{"id": "t1"}], "references": []}
    # No grids requested + references below the measure minimum → early return,
    # no exception, nothing to observe (pure no-op path).
    _warn_if_sidecars_outweigh_slice("doc-2", result, "a decently sized slice" * 10)


# ─── route_tables_mutation: the no-session branch ────────────────────────────


def _make_table(doc: Y.Doc, tid: str, matrix: list[list[str]]) -> None:
    tables = doc.get("tables", type=Y.Map)
    table = Y.Map()
    tables[tid] = table
    cols = Y.Array()
    table["columns"] = cols
    for _ in range(2):
        c = Y.Map()
        cols.append(c)
        c["w"] = 160
    rows = Y.Array()
    table["rows"] = rows
    for row in matrix:
        yrow = Y.Array()
        rows.append(yrow)
        for i in range(2):
            cell = Y.Map()
            yrow.append(cell)
            cell["t"] = Y.Text(row[i] if i < len(row) else "")


@pytest.mark.asyncio
async def test_route_tables_mutation_no_session_persists_and_publishes(monkeypatch):
    from agent import table_writes
    from collab import registry as collab_registry

    from table_serialize import read_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    doc_id = "r2b-table-doc"

    async def fake_load(_id):
        return doc

    published = []

    async def fake_publish(_id, update, append=True):
        published.append(update)

    emitted = []

    async def fake_emit(event, **kwargs):
        emitted.append((event, kwargs))

    def _no_session(kind, eid):
        return None

    monkeypatch.setattr("ydoc_store.load", fake_load)
    monkeypatch.setattr("ydoc_store.publish_doc_update", fake_publish)
    monkeypatch.setattr("event_bus.emit", fake_emit)
    monkeypatch.setattr(collab_registry, "get_active_session", _no_session)

    await table_writes.route_tables_mutation(
        doc_id=doc_id, table_id="t1", row=1, col=1, new_value="EDITED",
        project_id=None,
    )

    # The cell took the edit on the loaded doc
    assert str(read_table_cell(doc, "t1", 1, 1)) == "EDITED"
    # The update was published for other replicas (append-log path)
    assert len(published) == 1
    # content_flushed emitted — cell text feeds embeddings/search via expand_tables
    assert any(e[0] == "content_flushed" for e in emitted), emitted


