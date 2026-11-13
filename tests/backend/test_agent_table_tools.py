"""Tests for the agent `read_table` / `edit_table_cell` tools (plan
`1782998950958-agent-table-read-and-cell-edit.md`).

Unit-level, mirrors `test_chat_agent_multitool.py`: pure primitives + monkeypatched
DB/collab seams over a real pycrdt Doc fixture (no live DB/Redis).
"""

import asyncio
import json

import pycrdt as Y
import pytest
from fastapi import HTTPException
from helpers import cell_ids, cell_text, join_collab_ws, make_table, project_collab_url

from db import get_db

# ─── table_serialize primitives ──────────────────────────────────────────────


def test_read_table_cell_reads_and_bounds():
    from table_serialize import read_table_cell

    doc = Y.Doc()
    make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    rid, cid = cell_ids(doc, "t1", 1, 0)
    assert read_table_cell(doc, "t1", rid, cid) == "c"
    assert read_table_cell(doc, "t1", "no-row", cid) is None
    assert read_table_cell(doc, "t1", rid, "no-col") is None
    assert read_table_cell(doc, "unknown", rid, cid) is None


def test_set_table_cell_edits_text_in_place_not_replaced():
    """A live cell editor is yCollab-bound to the cell's EXACT Y.Text; the edit must
    fire that node's own observer (in-place), never swap in a fresh Text (which
    detaches the binding → agent edit invisible until page reload)."""
    from table_serialize import read_table_cell, set_table_cell

    doc = Y.Doc()
    make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    body = cell_text(doc, "t1", 1, 1)
    fired: list[object] = []
    _sub = body.observe(lambda e: fired.append(e))  # noqa: F841 — keep the sub alive

    assert set_table_cell(doc, "t1", *cell_ids(doc, "t1", 1, 1), "DDD") is True
    assert fired, "cell edit did not reach the bound Y.Text — Text was replaced, not edited"
    assert read_table_cell(doc, "t1", *cell_ids(doc, "t1", 1, 1)) == "DDD"
    # other cells untouched
    assert read_table_cell(doc, "t1", *cell_ids(doc, "t1", 0, 0)) == "a"


def test_set_table_cell_clears_to_empty_in_place():
    from table_serialize import read_table_cell, set_table_cell

    doc = Y.Doc()
    make_table(doc, "t1", [["a", "b"], ["c", "d"]])
    body = cell_text(doc, "t1", 0, 0)
    fired: list[object] = []
    _sub = body.observe(lambda e: fired.append(e))  # noqa: F841

    assert set_table_cell(doc, "t1", *cell_ids(doc, "t1", 0, 0), "") is True
    assert fired
    assert read_table_cell(doc, "t1", *cell_ids(doc, "t1", 0, 0)) == ""


def test_set_table_cell_out_of_bounds_is_noop():
    from table_serialize import set_table_cell

    doc = Y.Doc()
    make_table(doc, "t1", [["a"]])
    rid, cid = cell_ids(doc, "t1", 0, 0)
    assert set_table_cell(doc, "t1", "no-row", cid, "x") is False
    assert set_table_cell(doc, "t1", rid, "no-col", "x") is False


# ─── read_document tables field ──────────────────────────────────────────────


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


async def test_read_document_table_id_returns_full_grid(monkeypatch):
    from agent import readonly_executors as agent_module

    doc = Y.Doc()
    make_table(doc, "t1", [["Name", "Role"], ["Aragorn", "Ranger"]])
    make_table(doc, "t2", [["b"]])
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


async def test_validate_table_cell_edit_no_access_rejected(monkeypatch):
    from agent.table_writes import validate_table_cell_edit

    doc = Y.Doc()
    make_table(doc, "t1", [["a"]])
    _mk_apply_table_mocks(monkeypatch, doc=doc, access="readonly")

    with pytest.raises(HTTPException) as exc:
        await validate_table_cell_edit(
            document_id="doc-1",
            edits=[{"table_id": "t1", "row": 0, "col": 0, "old_value": "a"}],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 403


async def test_apply_edit_table_cell_noop_skips_checkpoint(monkeypatch):
    from agent.table_writes import apply_edit_table_cell

    doc = Y.Doc()
    make_table(doc, "t1", [["a"]])
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


# ─── structural primitives: add_table_rows / add_table_column / create_table_entry ─


def test_add_table_rows_empty_is_noop():
    from helpers import read_table_grid

    from table_serialize import add_table_rows

    doc = Y.Doc()
    make_table(doc, "t1", [["A"]])
    assert add_table_rows(doc, "t1", []) == []
    assert read_table_grid(doc, "t1")["rows"] == [["A"]]


def test_add_table_rows_missing_table_is_noop():
    from table_serialize import add_table_rows

    doc = Y.Doc()
    make_table(doc, "t1", [["A"]])
    # must not raise on a missing table id
    assert add_table_rows(doc, "no-such", [{"c": "x"}]) == []


def test_add_table_column_at_end_inserts_into_every_row():
    from helpers import read_table_grid

    from table_serialize import add_table_column

    doc = Y.Doc()
    make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    (r0, c1), (r1, _) = cell_ids(doc, "t1", 0, 1), cell_ids(doc, "t1", 1, 1)
    add_table_column(doc, "t1", before_id=c1, after_id=None, texts={r0: "AC", r1: "5"})
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["Name", "HP", "AC"], ["Goblin", "7", "5"]]
    assert grid["columns"] == [160, 160, 160]


def test_add_table_column_at_index_shifts_existing():
    from helpers import read_table_grid

    from table_serialize import add_table_column

    doc = Y.Doc()
    make_table(doc, "t1", [["A", "B"], ["1", "2"], ["3", "4"]])
    (r0, c0), (r1, _), (r2, _) = (cell_ids(doc, "t1", i, 0) for i in range(3))
    add_table_column(doc, "t1", before_id=None, after_id=c0, texts={r0: "X", r1: "p", r2: "q"})
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["X", "A", "B"], ["p", "1", "2"], ["q", "3", "4"]]


def test_add_table_column_pads_short_values():
    from helpers import read_table_grid

    from table_serialize import add_table_column

    doc = Y.Doc()
    make_table(doc, "t1", [["A"], ["1"], ["2"], ["3"]])
    (r0, c0), (r1, _) = cell_ids(doc, "t1", 0, 0), cell_ids(doc, "t1", 1, 0)
    add_table_column(doc, "t1", before_id=c0, after_id=None, texts={r0: "B", r1: "only"})
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [
        ["A", "B"], ["1", "only"], ["2", ""], ["3", ""],
    ]


def test_create_table_entry_builds_model_and_returns_width():
    from helpers import read_table_grid

    from table_serialize import create_table_entry

    doc = Y.Doc()
    doc.get("tables", type=Y.Map)
    matrix = [["Name", "Role"], ["Aragorn", "Ranger"], ["Legolas", "Elf"]]
    create_table_entry(doc, "newid", matrix)
    grid = read_table_grid(doc, "newid")
    assert grid["columns"] == [454, 390]
    assert grid["rows"] == matrix


def test_create_table_entry_ragged_pads():
    from helpers import read_table_grid

    from table_serialize import create_table_entry

    doc = Y.Doc()
    doc.get("tables", type=Y.Map)
    create_table_entry(doc, "tid", [["a", "b", "c"], ["x"]])
    grid = read_table_grid(doc, "tid")
    assert grid["rows"] == [["a", "b", "c"], ["x", "", ""]]


# ─── apply_add_table_rows / apply_add_table_column / apply_create_table ───────


async def test_apply_add_table_rows_appends_at_bottom(monkeypatch):
    from agent.table_writes import apply_add_table_rows
    from helpers import read_table_grid

    doc = Y.Doc()
    make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
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


async def test_apply_add_table_column_inserts_at_end(monkeypatch):
    from agent.table_writes import apply_add_table_column
    from helpers import read_table_grid

    doc = Y.Doc()
    make_table(doc, "t1", [["Name", "HP"], ["Goblin", "7"]])
    captured = _mk_apply_table_mocks(monkeypatch, doc=doc)

    result = await apply_add_table_column(
        doc_id="doc-1", table_id="t1", at_index=2, header="AC", values=["5"],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    grid = read_table_grid(doc, "t1")
    assert grid["rows"] == [["Name", "HP", "AC"], ["Goblin", "7", "5"]]
    assert captured["checkpoint"]["document_id"] == "doc-1"


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


# ─── import_file: a pipe table lands as an object (plan gfm-table-ingress) ────


# Before the conversion, a pipe table imported as an object hid its cell text
# from documents.content and from the mention rebuild (a `[t](<id>)` cell link
# silently lost its edge); the .txt twin pins the predicate.
# TEST-WHY: data-loss — an imported table's cell link must keep its mention edge; .txt stays text.
async def test_import_file_md_table_becomes_object_with_mention_edge(
    client, test_db, project_with_doc,
):
    from routes.tool_api.imports import _import_document

    from table_serialize import capture_tables_json
    from ydoc_store import load as load_ydoc

    pid, host_id, uid = project_with_doc
    user = {"user_id": uid, "name": "Table Test"}
    body = (
        "# Notes\n\n"
        "| what | link |\n"
        "| --- | --- |\n"
        f"| row | [t]({host_id}) |\n"
    )

    async def _import(filename: str) -> str:
        result = await _import_document(
            is_reference=False, binary_allowed=False, filename=filename,
            content=body, content_base64=None, title="Imported",
            parent_id=None, document_id=None, project_id=pid, user=user,
            scope_root=None,
        )
        assert result["status"] == "applied"
        return result["doc_id"]

    doc_id = await _import("notes.md")
    db = await get_db()
    row = (await db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": doc_id},
    ))[0]
    # The persisted row is the GFM read-model: table text, never a `table:`
    # anchor — embeddings/search read this column.
    assert "table:" not in row["content"]
    assert "| row |" in row["content"]

    # The tables map holds the matrix — the cell link lives in the MODEL.
    tables = json.loads(capture_tables_json(await load_ydoc(doc_id)))
    (model,) = tables.values()
    assert model["rows"] == [["what", "link"], ["row", f"[t]({host_id})"]]

    # The finalize tail ran on the EXPANDED body, so the cell link has its
    # mention edge (anchor-form text would have hidden it).
    edges = await db.query(
        "SELECT VALUE meta::id(out) FROM doc_mentions "
        "WHERE in = type::record('documents', $id)",
        {"id": doc_id},
    )
    assert host_id in list(edges or [])

    # The .txt twin: same body, non-markdown predicate — stays pipe text.
    txt_id = await _import("notes.txt")
    txt_tables = capture_tables_json(await load_ydoc(txt_id))
    assert txt_tables == "{}"
    txt_row = (await db.query(
        "SELECT content FROM type::record('documents', $id)", {"id": txt_id},
    ))[0]
    assert "| row | [t](" + host_id + ") |" in txt_row["content"]


# ─── agent-authored writes: pipe tables become objects (plan gfm-table-ingress) ──


@pytest.fixture
def _clear_sessions():
    """Own no live CollabSession across the test, and drop the ones it made —
    mirror of test_agent_batch_edit's fixture (a leaked periodic-flush task
    wedges the worker)."""
    from collab.registry import _sessions

    _sessions.clear()
    yield
    for session in _sessions.values():
        session.stop_periodic_flush()
        if session._batch_task and not session._batch_task.done():
            session._batch_task.cancel()
    _sessions.clear()


def _set_session_text(session, text: str) -> None:
    """Replace the live session's buffer (mirror of test_agent_batch_edit)."""
    t = session._get_text()
    old_len = len(t)
    if old_len > 0:
        del t[0:old_len]
    if text:
        t += text


_SEED_TEXT = "Intro paragraph.\n\n![Existing](table:t0)\n\nTail line."
_SEED_TABLES = {"t0": {"columns": [0, 0], "rows": [["h1", "h2"], ["v1", "v2"]]}}
_TABLE_EDIT = {
    "old_string": "Tail line.",
    "new_string": "Tail line.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |",
}


def _assert_table_object_landed(result, live: str, tables: dict) -> None:
    """The inserted pipe table left an anchor + a model (and the doc's own
    table survived the same update); the pipe rows are gone from the text."""
    (table_id,) = [t["table_id"] for t in result["tables"]]
    assert result["tables"] == [{"table_id": table_id, "label": "Table"}]
    assert f"![Table](table:{table_id})" in live
    assert "| 1 | 2 |" not in live
    assert tables[table_id]["rows"] == [["a", "b"], ["1", "2"]]
    assert tables["t0"]["rows"] == [["h1", "h2"], ["v1", "v2"]]


# TEST-WHY: cross-process-contract — a whole-table edit lands anchor+model in one update over the real store, live session and none.
@pytest.mark.parametrize("live_session", [True, False], ids=["live", "no-session"])
def test_edit_document_inserted_table_becomes_object(
    sync_app, collab_project, _clear_sessions, live_session,
):
    """edit_document splicing a whole pipe table on its own lines: the anchor
    and the model land in ONE update — through the live collab session and on
    the no-session persistence path alike; a table already in the doc rides the
    same update untouched."""
    from agent.tool_api_surface import apply_edits_to_document

    from table_serialize import capture_tables_json, create_table_entry
    from ydoc_store import load as load_ydoc
    from ydoc_store import set_content

    pid, doc_id, admin_token, _ut, admin_uid, _uu = collab_project
    loop = asyncio.get_event_loop()

    async def _apply():
        return await apply_edits_to_document(
            doc_id=doc_id, project_id=pid, user={"user_id": admin_uid},
            edits=[dict(_TABLE_EDIT)],
        )

    if not live_session:
        loop.run_until_complete(set_content(
            doc_id, _SEED_TEXT, persist=True,
            tables_json=json.dumps(_SEED_TABLES),
        ))
        result = loop.run_until_complete(_apply())
        doc = loop.run_until_complete(load_ydoc(doc_id))
        _assert_table_object_landed(
            result, str(doc.get("content", type=Y.Text)),
            json.loads(capture_tables_json(doc)),
        )
        # The persisted row is the GFM read-model: table text, never a
        # `table:` anchor — embeddings/search read this column.
        db = loop.run_until_complete(get_db())
        rows = loop.run_until_complete(db.query(
            "SELECT content FROM type::record('documents', $id)", {"id": doc_id},
        ))
        assert "table:" not in rows[0]["content"]
        assert "| 1 | 2 |" in rows[0]["content"]
        return

    with sync_app.websocket_connect(
        project_collab_url(pid), cookies={"lore_session": admin_token},
    ) as ws:
        join_collab_ws(ws, doc_id)
        ws.receive_text()
        from collab.registry import _session_key, _sessions

        session = _sessions[_session_key("doc", doc_id)]
        _set_session_text(session, _SEED_TEXT)
        create_table_entry(session.ydoc, "t0", _SEED_TABLES["t0"]["rows"])
        result = loop.run_until_complete(_apply())
        _assert_table_object_landed(
            result, str(session._get_text()),
            json.loads(capture_tables_json(session.ydoc)),
        )


# TEST-WHY: cross-process-contract — partial, fenced and mid-line splices stay pipe text over the real store, tables map untouched.
@pytest.mark.parametrize(
    "content,old_string,new_string",
    [
        ("| a | b |\n| --- | --- |\n| 1 | 2 |", "| a | b |", "| x | y |"),
        ("Before.\n\n```lang\n| a | b |\n| --- | --- |\n```\n\nAfter.",
         "| a | b |", "| x | y |"),
        ("Some text here.", "text", "text | a |\n| --- |"),
    ],
    ids=["legacy-header", "inside-fence", "mid-line"],
)
async def test_edit_document_partial_splices_stay_pipe_text(
    collab_project, _clear_sessions, content, old_string, new_string,
):
    """The skip rule: a splice that is a legacy table's fragment, sits inside a
    fence, or starts mid-line never converts — the authored text lands verbatim
    and the tables map is untouched."""
    from agent.tool_api_surface import apply_edits_to_document
    from collab.registry import _session_key, _sessions

    from table_serialize import capture_tables_json
    from ydoc_store import load as load_ydoc
    from ydoc_store import set_content

    pid, doc_id, _at, _ut, admin_uid, _uu = collab_project
    await set_content(doc_id, content, persist=True)
    _sessions.pop(_session_key("doc", doc_id), None)

    result = await apply_edits_to_document(
        doc_id=doc_id, project_id=pid, user={"user_id": admin_uid},
        edits=[{"old_string": old_string, "new_string": new_string}],
    )

    assert result["status"] == "applied"
    assert "tables" not in result
    doc = await load_ydoc(doc_id)
    live = str(doc.get("content", type=Y.Text))
    # the authored pipe text landed verbatim (block-boundary aligned), nothing
    # converted: no anchor, the tables map untouched
    assert new_string in live
    assert "](table:" not in live
    assert capture_tables_json(doc) == "{}"


# TEST-WHY: cross-process-contract — an authored cell over the cap answers 400 and no row exists in the real store.
async def test_agent_create_oversize_table_cell_400_writes_nothing(
    test_db, project_with_doc,
):
    """create_document with a cell over AGENT_TABLE_MAX_CELL_CHARS: the refusal
    fires BEFORE the row birth — 400, nothing written (never silent
    truncation)."""
    from routes.tool_api._common import _apply_create_document_direct

    from config import AGENT_TABLE_MAX_CELL_CHARS

    pid, _idx, uid = project_with_doc
    oversize = "x" * (AGENT_TABLE_MAX_CELL_CHARS + 1)
    with pytest.raises(HTTPException) as exc:
        await _apply_create_document_direct(
            title="Never born",
            content=f"| a | b |\n| --- | --- |\n| 1 | {oversize} |",
            parent_id=None, project_id=pid,
            user={"user_id": uid, "name": "t"}, media_type="markdown",
        )
    assert exc.value.status_code == 400
    db = await get_db()
    rows = await db.query(
        "SELECT id FROM documents WHERE title = 'Never born'")
    assert not rows


def _merge_b2_c3(doc, table_id: str) -> None:
    from helpers import cell_ids

    from table_serialize import cell_key

    table = doc.get("tables", type=Y.Map)[table_id]
    er, ec = cell_ids(doc, table_id, 2, 2)
    with doc.transaction():
        table["merges"] = Y.Map({cell_key(*cell_ids(doc, table_id, 1, 1)): Y.Map({"r": er, "c": ec})})


# TEST-WHY: cross-process-contract — the agent must see merged areas by index and never write text the editor hides under one.
async def test_agent_merged_area_read_and_refusals(monkeypatch):
    from agent import readonly_executors as agent_module
    from agent.table_writes import apply_add_table_column, resolve_cell
    from fastapi import HTTPException
    from helpers import read_table_grid

    doc = Y.Doc()
    make_table(doc, "t1", [["A", "B", "C"], ["1", "x", ""], ["2", "", ""]])
    _merge_b2_c3(doc, "t1")
    _mk_read_table_mocks(monkeypatch, doc=doc, content="")
    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"}, table_id="t1",
    )
    assert out["tables"][0]["merges"] == [{"row": 1, "col": 1, "rows": 2, "cols": 2}]

    assert resolve_cell(doc, "t1", 1, None, 1)[2] == 1
    with pytest.raises(HTTPException) as exc:
        resolve_cell(doc, "t1", 2, "C", None)
    assert exc.value.status_code == 400 and exc.value.detail["anchor"] == {"row": 1, "col": 1}

    _mk_apply_table_mocks(monkeypatch, doc=doc)
    with pytest.raises(HTTPException) as exc:
        await apply_add_table_column(
            doc_id="doc-1", table_id="t1", at_index=2, header="H", values=["", "v"],
            project_id="p-1", user={"user_id": "u-1"},
        )
    assert exc.value.status_code == 400
    result = await apply_add_table_column(
        doc_id="doc-1", table_id="t1", at_index=2, header="H", values=[],
        project_id="p-1", user={"user_id": "u-1"},
    )
    assert result["status"] == "applied"
    assert read_table_grid(doc, "t1")["rows"][0] == ["A", "B", "H", "C"]
    out = await agent_module._read_document_exec(
        doc_id="doc-1", project_id="p-1", user={"user_id": "u-1"}, table_id="t1",
    )
    assert out["tables"][0]["merges"] == [{"row": 1, "col": 1, "rows": 2, "cols": 3}]
