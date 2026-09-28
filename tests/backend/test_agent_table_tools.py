"""Tests for the agent `read_table` / `edit_table_cell` tools (plan
`1782998950958-agent-table-read-and-cell-edit.md`).

Unit-level, mirrors `test_chat_agent_multitool.py`: pure primitives + monkeypatched
DB/collab seams over a real pycrdt Doc fixture (no live DB/Redis).
"""
import asyncio

import pycrdt as Y
import pytest
from fastapi import HTTPException


def _make_table(doc: Y.Doc, tid: str, matrix: list[list[str]], widths: list[int] | None = None) -> None:
    """Build a `tables` entry mirroring the frontend createTable shape (see
    test_table_capture.py's `_make_table`)."""
    tables = doc.get("tables", type=Y.Map)
    table = Y.Map()
    tables[tid] = table
    cols = Y.Array()
    table["columns"] = cols
    width = max(len(r) for r in matrix) if matrix else 0
    for i in range(width):
        c = Y.Map()
        cols.append(c)
        c["w"] = (widths or [160] * width)[i]
    rows = Y.Array()
    table["rows"] = rows
    for row in matrix:
        yrow = Y.Array()
        rows.append(yrow)
        for i in range(width):
            cell = Y.Map()
            yrow.append(cell)
            cell["t"] = Y.Text(row[i] if i < len(row) else "")


# ─── table_serialize primitives ──────────────────────────────────────────────


def test_read_table_grid_returns_columns_and_rows():
    from table_serialize import read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "Role"], ["Aragorn", "Ranger"]], [200, 300])
    assert read_table_grid(doc, "t1") == {
        "columns": [200, 300], "rows": [["Name", "Role"], ["Aragorn", "Ranger"]],
    }


def test_read_table_grid_missing_id_returns_none():
    from table_serialize import read_table_grid

    doc = Y.Doc()
    assert read_table_grid(doc, "missing") is None


def test_read_table_cell_reads_and_bounds():
    from table_serialize import read_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    assert read_table_cell(doc, "t1", 1, 0) == "c"
    assert read_table_cell(doc, "t1", 5, 0) is None  # row OOB
    assert read_table_cell(doc, "t1", 0, 5) is None  # col OOB
    assert read_table_cell(doc, "unknown", 0, 0) is None


def test_set_table_cell_edits_text_in_place_not_replaced():
    """A live cell editor is yCollab-bound to the cell's EXACT Y.Text; the edit must
    fire that node's own observer (in-place), never swap in a fresh Text (which
    detaches the binding → agent edit invisible until page reload)."""
    from table_serialize import read_table_cell, set_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    body = doc.get("tables", type=Y.Map)["t1"]["rows"][1][1]["t"]
    fired: list[object] = []
    _sub = body.observe(lambda e: fired.append(e))  # noqa: F841 — keep the sub alive

    assert set_table_cell(doc, "t1", 1, 1, "DDD") is True
    assert fired, "cell edit did not reach the bound Y.Text — Text was replaced, not edited"
    assert read_table_cell(doc, "t1", 1, 1) == "DDD"
    # other cells untouched
    assert read_table_cell(doc, "t1", 0, 0) == "a"


def test_set_table_cell_clears_to_empty_in_place():
    from table_serialize import read_table_cell, set_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    body = doc.get("tables", type=Y.Map)["t1"]["rows"][0][0]["t"]
    fired: list[object] = []
    _sub = body.observe(lambda e: fired.append(e))  # noqa: F841

    assert set_table_cell(doc, "t1", 0, 0, "") is True
    assert fired
    assert read_table_cell(doc, "t1", 0, 0) == ""


def test_set_table_cell_out_of_bounds_is_noop():
    from table_serialize import set_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    assert set_table_cell(doc, "t1", 9, 9, "x") is False


def test_extract_table_labels_reads_anchor():
    from table_serialize import extract_table_labels

    content = "intro\n![Characters](table:t1)\nmore\n![Places](table:t2)"
    assert extract_table_labels(content) == {"t1": "Characters", "t2": "Places"}


# ─── read_document tables field (Step 2c: read_table folded in) ──────────────


def _mk_read_table_mocks(monkeypatch, *, doc, content, access="full", project_id="p-1",
                          doc_project_id="p-1"):
    from agent import readonly_executors as agent_module

    async def fake_fetch_one(_t, _r):
        return {"project_id": doc_project_id, "title": "T"}

    async def fake_access(_d, _u):
        return access

    async def fake_resolve_live_doc_state(_doc_id):
        return content, "{}"

    monkeypatch.setattr(agent_module, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(agent_module, "get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    import collab.registry as collab_module
    monkeypatch.setattr(collab_module, "get_active_session", lambda *_a: None)

    async def fake_load_ydoc(_id):
        return doc

    import ydoc_store
    monkeypatch.setattr(ydoc_store, "load", fake_load_ydoc)

    # The D6 `references[]` sidecar is a live DB query (SELECT ... FROM documents
    # WHERE parent_id = $host) and this module's charter is "no live DB/Redis"
    # (docstring at top). Without this patch the query falls through to the real
    # get_db pool and dies with NotFoundError("The table 'documents' does not
    # exist") whenever this module lands in an xdist worker where the
    # session-scoped test_db fixture (which patches get_db globally across
    # sys.modules) has not been instantiated yet — the ordering dependency CI
    # run #1075 exposed (Backend Tests went green→red with no change to this
    # module, purely from worker-distribution shifts).
    async def fake_references(_doc_id):
        return []

    monkeypatch.setattr(agent_module, "_build_references_field", fake_references)


async def test_read_document_default_returns_table_index(monkeypatch):
    """tables default "index" → INDEX only (table_id + label + n_cols, no `rows`) so
    a weak agent does not dump every full grid into context (Step 2c fold of
    read_table's index-only INVARIANT)."""
    from agent import readonly_executors as agent_module

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "Role"], ["Aragorn", "Ranger"]])
    content = "intro\n![Characters](table:t1)"
    _mk_read_table_mocks(monkeypatch, doc=doc, content=content)

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert out["doc_id"] == "doc-1"
    assert len(out["tables"]) == 1
    t = out["tables"][0]
    assert t["table_id"] == "t1"
    assert t["label"] == "Characters"
    assert t["n_cols"] == 2
    assert "rows" not in t


async def test_read_document_table_id_returns_full_grid(monkeypatch):
    from agent import readonly_executors as agent_module

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "Role"], ["Aragorn", "Ranger"]])
    _make_table(doc, "t2", [["b"]])
    _mk_read_table_mocks(monkeypatch, doc=doc, content="")

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"}, table_id="t1",
    )
    assert [t["table_id"] for t in out["tables"]] == ["t1"]
    assert out["tables"][0]["rows"] == [
        {"row": 0, "cells": ["Name", "Role"]},
        {"row": 1, "cells": ["Aragorn", "Ranger"]},
    ]

    out_missing = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"}, table_id="no-such-id",
    )
    assert out_missing["tables"] == []


async def test_read_document_rejects_no_access(monkeypatch):
    from agent import readonly_executors as agent_module

    doc = Y.Doc()
    _mk_read_table_mocks(monkeypatch, doc=doc, content="", access=None)

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert "error" in out


async def test_read_document_rejects_cross_project(monkeypatch):
    from agent import readonly_executors as agent_module

    doc = Y.Doc()
    _mk_read_table_mocks(monkeypatch, doc=doc, content="", doc_project_id="p-OTHER")

    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"},
    )
    assert "error" in out


# ─── validate_table_cell_edit / apply_edit_table_cell ────────────────────────


def _mk_apply_table_mocks(monkeypatch, *, doc, access="full", project_id="p-1",
                           doc_project_id="p-1", captured=None, content="content"):
    from agent import collab_writes as cw

    captured = captured if captured is not None else {}

    async def fake_fetch_one(_t, _r):
        return {"project_id": doc_project_id}

    async def fake_access(_d, _u):
        return access

    async def fake_resolve_live_doc_state(_doc_id):
        return content, "{}"

    async def fake_checkpoint(**kw):
        captured["checkpoint"] = kw
        return {"checkpoint_id": "cp-1"}

    # M7: the fetch+RBAC+scope gate moved into scope.gate_mutation_target, which
    # resolves fetch_one / get_document_access lazily from their source modules — patch
    # THOSE (tus no longer imports them).
    monkeypatch.setattr("db.fetch_one", fake_fetch_one)
    monkeypatch.setattr("access.get_document_access", fake_access)
    monkeypatch.setattr("agent.doc_state.resolve_live_doc_state", fake_resolve_live_doc_state)
    monkeypatch.setattr(cw, "_create_agent_pre_edit_checkpoint", fake_checkpoint)

    import collab.registry as collab_module
    monkeypatch.setattr(collab_module, "get_active_session", lambda *_a: None)

    async def fake_load_ydoc(_id):
        return doc

    import ydoc_store
    monkeypatch.setattr(ydoc_store, "load", fake_load_ydoc)

    async def fake_publish(_id, _update):
        captured["published"] = True

    monkeypatch.setattr(ydoc_store, "publish_doc_update", fake_publish)
    return captured


async def test_validate_table_cell_edit_stale_carries_current_value(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 1, "col": 0, "old_value": "WRONG"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["current_value"] == "c"


async def test_validate_table_cell_edit_empty_cell_old_value_resolves(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", ""], ["c", "d"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    # old_value="" for an empty cell must resolve WITHOUT raising.
    result = await validate_table_cell_edit(
        document_id="doc-1",
        edits=[{"table_id": "t1", "row": 0, "col": 1, "old_value": ""}],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result == "{}"


async def test_validate_table_cell_edit_oob_carries_bounds(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 9, "col": 0, "old_value": "x"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["n_rows"] == 2
    assert exc.value.detail["n_cols"] == 2


async def test_validate_table_cell_edit_by_header_name_resolves_col(monkeypatch):
    """3A: a header-NAME `column` resolves to its numeric index and is written back
    onto the edit dict, overriding the numeric `col` fallback."""
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    edit = {"table_id": "t1", "row": 1, "col": 0, "column": "HP", "old_value": "7"}
    result = await validate_table_cell_edit(
        document_id="doc-1", edits=[edit], project_id="p-1", user={"user_id": "u-1"},
    )
    assert result == "{}"
    assert edit["col"] == 1, "column='HP' must resolve to index 1, overriding col=0"


async def test_validate_table_cell_edit_by_header_name_col_absent(monkeypatch):
    """Real Pi-bridge shape: the model sends only `column` (col un-advertised), so the
    edit dict carries `col=None`. Resolution must succeed off `column` alone — never
    TypeError on the None fallback."""
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    edit = {"table_id": "t1", "row": 1, "col": None, "column": "HP", "old_value": "7"}
    result = await validate_table_cell_edit(
        document_id="doc-1", edits=[edit], project_id="p-1", user={"user_id": "u-1"},
    )
    assert result == "{}"
    assert edit["col"] == 1


def test_edit_table_cell_schema_requires_column():
    """`column` MUST be a required parameter of EACH advertised edit item — col/column
    both optional let weak models fill an empty cell (old_value="") addressing by row
    alone, producing a proposal with no coordinate that 500s at apply
    (edit-table-cell-column-apply-fix live recurrence). The batch schema advertises
    edits[]; the per-item required set binds the contract (and `col` is NOT advertised)."""
    from agent.tools import AGENT_TOOLS

    tool = next(t for t in AGENT_TOOLS if t["function"]["name"] == "edit_table_cell")
    params = tool["function"]["parameters"]
    assert params["required"] == ["document_id", "edits"]
    item = params["properties"]["edits"]["items"]
    assert "column" in item["required"]
    # INVARIANT: the numeric `col` is NOT advertised anywhere (dispatch-only fallback).
    assert "col" not in item["properties"]


async def test_resolve_table_col_no_coord_raises_400():
    """Neither a `column` name nor a `col` index → HTTPException(400), never a
    TypeError→500 (the confirm-route self-correct contract)."""
    from agent.table_writes import _resolve_table_col

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    with pytest.raises(HTTPException) as exc:
        _resolve_table_col(doc, "t1", None, None, 2, 2)
    assert exc.value.status_code == 400


async def test_validate_table_cell_edit_unknown_column_name(monkeypatch):
    """3A: a header name that does not exist → 404 carrying the available headers."""
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 1, "col": 0, "column": "Mana", "old_value": "7"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404
    assert exc.value.detail["headers"] == ["Name", "HP"]


async def test_validate_table_cell_edit_unknown_table_id(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "no-such", "row": 0, "col": 0, "old_value": "x"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404


async def test_validate_table_cell_edit_no_access_rejected(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc, access="readonly")

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 0, "col": 0, "old_value": "a"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 403


async def test_validate_table_cell_edit_cross_project_rejected(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc, doc_project_id="p-OTHER")

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 0, "col": 0, "old_value": "a"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404


async def test_apply_edit_table_cell_mutates_and_persists(monkeypatch):
    from agent.table_writes import apply_edit_table_cell

    from table_serialize import read_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc)

    result = await apply_edit_table_cell(
        edits=[{
            "document_id": "doc-1", "table_id": "t1", "row": 1, "col": 1,
            "old_value": "d", "new_value": "DDD",
        }],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert read_table_cell(doc, "t1", 1, 1) == "DDD"
    assert captured["published"] is True
    assert captured["checkpoint"]["document_id"] == "doc-1"


async def test_apply_edit_table_cell_noop_skips_checkpoint(monkeypatch):
    from agent.table_writes import apply_edit_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc)

    result = await apply_edit_table_cell(
        edits=[{
            "document_id": "doc-1", "table_id": "t1", "row": 0, "col": 0,
            "old_value": "a", "new_value": "a",
        }],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result == {"status": "applied", "noop": True, "document_id": "doc-1"}
    assert "checkpoint" not in captured


async def test_apply_edit_table_cell_stale_raises_before_mutating(monkeypatch):
    from agent.table_writes import apply_edit_table_cell


    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await apply_edit_table_cell(
            edits=[{
                "document_id": "doc-1", "table_id": "t1", "row": 0, "col": 0,
                "old_value": "WRONG", "new_value": "x",
            }],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 409


# ─── doc-edit-lock: parallel table-cell edits (Fix A.3) ──────────────────────


async def test_table_cell_parallel_edits_all_apply(monkeypatch):
    """2 cell edits to DIFFERENT cells of the same table, issued via gather — both
    must apply. Fix A.3 wraps apply_edit_table_cell in the SAME per-doc lock as text
    edits (a table-cell edit and a text edit on one doc must serialize through the
    document's convergence tail). This confirms the table path applies cleanly under
    the lock — no deadlock / over-serialization."""
    from agent import collab_writes as cw
    from agent.table_writes import apply_edit_table_cell

    from table_serialize import read_table_cell

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    async def fake_presence(*_a, **_kw):
        return None

    monkeypatch.setattr(cw, "broadcast_agent_presence", fake_presence)

    results = await asyncio.gather(
        apply_edit_table_cell(
            edits=[{
                "document_id": "doc-1", "table_id": "t1", "row": 0, "col": 0,
                "old_value": "a", "new_value": "AAA",
            }],
            project_id="p-1", user={"user_id": "u-1"},
        ),
        apply_edit_table_cell(
            edits=[{
                "document_id": "doc-1", "table_id": "t1", "row": 1, "col": 1,
                "old_value": "d", "new_value": "DDD",
            }],
            project_id="p-1", user={"user_id": "u-1"},
        ),
    )

    assert all(r["status"] == "applied" for r in results)
    assert read_table_cell(doc, "t1", 0, 0) == "AAA"
    assert read_table_cell(doc, "t1", 1, 1) == "DDD"


# ─── structural primitives: add_table_rows / add_table_column / create_table_entry ─


def test_add_table_rows_appends_at_bottom():
    from table_serialize import add_table_rows, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["A", "B"], ["1", "2"]])
    add_table_rows(doc, "t1", [["3", "4"], ["5", "6"]], width=2)
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["A", "B"], ["1", "2"], ["3", "4"], ["5", "6"]]


def test_add_table_rows_pads_shorter_row():
    from table_serialize import add_table_rows, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["A", "B"]])
    add_table_rows(doc, "t1", [["only-one"]], width=2)
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["A", "B"], ["only-one", ""]]


def test_add_table_rows_empty_is_noop():
    from table_serialize import add_table_rows, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["A"]])
    add_table_rows(doc, "t1", [], width=1)
    assert read_table_grid(doc, "t1")["rows"] == [["A"]]


def test_add_table_rows_missing_table_is_noop():
    from table_serialize import add_table_rows

    doc = Y.Doc()
    _make_table(doc, "t1", [["A"]])
    # must not raise on a missing table id
    add_table_rows(doc, "no-such", [["x"]], width=1)


def test_add_table_column_at_end_inserts_into_every_row():
    from table_serialize import add_table_column, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    add_table_column(doc, "t1", at_index=2, header="AC", values=["5"], n_rows=2)
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["Name", "HP", "AC"], ["Goblin", "7", "5"]]
    assert grid["columns"] == [160, 160, 160]


def test_add_table_column_at_index_shifts_existing():
    from table_serialize import add_table_column, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["A", "B"], ["1", "2"], ["3", "4"]])
    add_table_column(doc, "t1", at_index=0, header="X", values=["p", "q"], n_rows=3)
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["X", "A", "B"], ["p", "1", "2"], ["q", "3", "4"]]


def test_add_table_column_pads_short_values():
    from table_serialize import add_table_column, read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["A"], ["1"], ["2"], ["3"]])
    add_table_column(doc, "t1", at_index=1, header="B", values=["only"], n_rows=4)
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [
        ["A", "B"], ["1", "only"], ["2", ""], ["3", ""],
    ]


def test_create_table_entry_builds_model_and_returns_width():
    from table_serialize import create_table_entry, read_table_grid

    doc = Y.Doc()
    doc.get("tables", type=Y.Map)
    matrix = [["Name", "Role"], ["Aragorn", "Ranger"], ["Legolas", "Elf"]]
    create_table_entry(doc, "newid", matrix)
    grid = read_table_grid(doc, "newid")
    assert grid["columns"] == [160, 160]
    assert grid["rows"] == matrix


def test_create_table_entry_ragged_pads():
    from table_serialize import create_table_entry, read_table_grid

    doc = Y.Doc()
    doc.get("tables", type=Y.Map)
    create_table_entry(doc, "tid", [["a", "b", "c"], ["x"]])
    grid = read_table_grid(doc, "tid")
    assert grid["rows"] == [["a", "b", "c"], ["x", "", ""]]


# ─── apply_add_table_rows / apply_add_table_column / apply_create_table ───────


async def test_apply_add_table_rows_appends_at_bottom(monkeypatch):
    from agent.table_writes import apply_add_table_rows

    from table_serialize import read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc)

    result = await apply_add_table_rows(
        doc_id="doc-1", table_id="t1",
        rows_matrix=[["Orc", "8"], ["Warg", "9"]],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["Name", "HP"], ["Goblin", "7"], ["Orc", "8"], ["Warg", "9"]]
    assert captured["checkpoint"]["document_id"] == "doc-1"
    assert captured["published"] is True


async def test_apply_add_table_rows_empty_raises_400(monkeypatch):
    from agent.table_writes import apply_add_table_rows

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await apply_add_table_rows(
            doc_id="doc-1", table_id="t1", rows_matrix=[],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 400


async def test_apply_add_table_rows_over_length_row_400(monkeypatch):
    from agent.table_writes import apply_add_table_rows

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await apply_add_table_rows(
            doc_id="doc-1", table_id="t1", rows_matrix=[["x", "y", "z"]],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 400


async def test_apply_add_table_rows_unknown_table_404(monkeypatch):
    from agent.table_writes import apply_add_table_rows

    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await apply_add_table_rows(
            doc_id="doc-1", table_id="no-such", rows_matrix=[["x"]],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404


async def test_apply_add_table_column_inserts_at_end(monkeypatch):
    from agent.table_writes import apply_add_table_column

    from table_serialize import read_table_grid

    doc = Y.Doc()
    _make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc)

    result = await apply_add_table_column(
        doc_id="doc-1", table_id="t1", at_index=2, header="AC", values=["5"],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["Name", "HP", "AC"], ["Goblin", "7", "5"]]
    assert captured["checkpoint"]["document_id"] == "doc-1"


async def test_apply_add_table_column_bad_index_400(monkeypatch):
    from agent.table_writes import apply_add_table_column

    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc)

    with pytest.raises(HTTPException) as exc:
        await apply_add_table_column(
            doc_id="doc-1", table_id="t1", at_index=5, header="x", values=[],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 400


async def test_apply_create_table_builds_anchor_and_model(monkeypatch):
    from agent.table_writes import apply_create_table

    from table_serialize import extract_table_labels, read_table_grid

    doc = Y.Doc()
    # non-empty content at the doc tail — set on BOTH the resolve snapshot and the doc
    content = "Some intro text."
    doc.get("content", type=Y.Text).insert(0, content)
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc, content=content)

    result = await apply_create_table(
        doc_id="doc-1", label="Monsters",
        rows_matrix=[["Name", "HP"], ["Goblin", "7"]],
        section=None, project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    new_id = result["table_id"]
    # the anchor was spliced into content
    final = str(doc.get("content", type=Y.Text))
    labels = extract_table_labels(final)
    assert labels[new_id] == "Monsters"
    # the model was built under the fresh id
    grid = read_table_grid(doc, new_id)
    assert grid["rows"] == [["Name", "HP"], ["Goblin", "7"]]
    assert grid["columns"] == [160, 160]
    assert captured["checkpoint"]["document_id"] == "doc-1"
    assert captured["published"] is True


async def test_apply_create_table_unknown_section_404(monkeypatch):
    from agent.table_writes import apply_create_table

    doc = Y.Doc()
    content = "Some text."
    doc.get("content", type=Y.Text).insert(0, content)
    _mk_apply_table_mocks(monkeypatch, doc=doc, content=content)

    with pytest.raises(HTTPException) as exc:
        await apply_create_table(
            doc_id="doc-1", label="T",
            rows_matrix=[["a"]],
            section="No Such Heading",
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 404


async def test_apply_create_table_section_appends_at_section_end(monkeypatch):
    from agent.table_writes import apply_create_table

    from table_serialize import extract_table_labels

    doc = Y.Doc()
    content = "# Chapter\n\nIntro line.\n\n# Other\n\nBody."
    doc.get("content", type=Y.Text).insert(0, content)
    _mk_apply_table_mocks(monkeypatch, doc=doc, content=content)

    result = await apply_create_table(
        doc_id="doc-1", label="Table",
        rows_matrix=[["a", "b"]],
        section="Chapter",
        project_id="p-1", user={"user_id": "u-1"},
    )
    new_id = result["table_id"]
    final = str(doc.get("content", type=Y.Text))
    # the anchor lands at the end of the Chapter section, before "# Other"
    anchor_at = final.index(f"![Table](table:{new_id})")
    other_at = final.index("# Other")
    assert anchor_at < other_at
    assert extract_table_labels(final)[new_id] == "Table"


