"""`capture_tables_json` / `apply_tables_json` — full table-state capture (Order step 1).

The editable table block stores its model as a Yjs subtree (`getMap('tables')`) SIBLING
to the `content` Text. A checkpoint must capture that subtree completely (table ids,
column widths, cell text) so a restore rebuilds tables identically.

INVARIANT (shared shape, frontend ↔ backend): both producers emit the identical JSON
shape `{ columns: number[], rows: string[][] }` per table. This test pins the backend
producer; `frontend table-block-model.test.ts` pins the frontend producer and uses the
SAME fixture so a silent column-width drop fails loudly.
"""
import json

import pycrdt as Y
import pytest

from table_serialize import DEFAULT_COLUMN_WIDTH, apply_tables_json, capture_tables_json


# INVARIANT (cross-producer default column width): MUST equal the frontend
# DEFAULT_COLUMN_WIDTH (frontend table-block-model.ts), pinned there in
# table-block-model.test.ts. A drift makes a restored table render at the wrong width.
def test_default_column_width_matches_frontend_constant():
    assert DEFAULT_COLUMN_WIDTH == 160


def _make_table(doc: Y.Doc, tid: str, matrix: list[list[str]], widths: list[int]) -> None:
    """Build a `tables` entry mirroring the frontend createTable + setColumnWidth shape."""
    tables = doc.get("tables", type=Y.Map)
    table = Y.Map()
    tables[tid] = table
    cols = Y.Array()
    table["columns"] = cols
    width = max(len(r) for r in matrix) if matrix else 0
    for i in range(width):
        c = Y.Map()
        cols.append(c)
        c["w"] = widths[i] if i < len(widths) else 160
    rows = Y.Array()
    table["rows"] = rows
    for row in matrix:
        yrow = Y.Array()
        rows.append(yrow)
        for i in range(width):
            cell = Y.Map()
            yrow.append(cell)
            cell["t"] = Y.Text(row[i] if i < len(row) else "")


# ── capture_tables_json ────────────────────────────────────────────────────────


def test_capture_empty_doc_is_empty_object():
    doc = Y.Doc()
    assert capture_tables_json(doc) == "{}"


def test_capture_no_tables_root_is_empty_object():
    doc = Y.Doc()
    text = doc.get("content", type=Y.Text)
    text += "plain, no tables map present"
    assert capture_tables_json(doc) == "{}"


def test_capture_emits_columns_and_rows():
    doc = Y.Doc()
    _make_table(doc, "t1", [["a", "b"], ["c", "d"]], [200, 320])
    data = json.loads(capture_tables_json(doc))
    assert data == {"t1": {"columns": [200, 320], "rows": [["a", "b"], ["c", "d"]]}}


def test_capture_multiple_tables():
    doc = Y.Doc()
    _make_table(doc, "t1", [["a"]], [100])
    _make_table(doc, "t2", [["x", "y", "z"]], [10, 20, 30])
    data = json.loads(capture_tables_json(doc))
    assert set(data) == {"t1", "t2"}
    assert data["t1"] == {"columns": [100], "rows": [["a"]]}
    assert data["t2"] == {"columns": [10, 20, 30], "rows": [["x", "y", "z"]]}


def test_capture_preserves_special_chars_and_empty_cells():
    matrix = [["a|b", ""], ["c\nd", "  e  "]]
    doc = Y.Doc()
    _make_table(doc, "t9", matrix, [50, 60])
    data = json.loads(capture_tables_json(doc))
    # Cell bodies are read VERBATIM (no GFM escaping) — capture is lossless, unlike export.
    assert data["t9"]["rows"] == matrix
    assert data["t9"]["columns"] == [50, 60]


# ── apply_tables_json ──────────────────────────────────────────────────────────


def test_apply_none_is_noop():
    doc = Y.Doc()
    _make_table(doc, "keep", [["a"]], [99])
    apply_tables_json(doc, None)
    tables = doc.get("tables", type=Y.Map)
    assert "keep" in tables  # untouched on legacy (None) restore


def test_apply_empty_object_clears_existing_tables():
    doc = Y.Doc()
    _make_table(doc, "gone", [["a"]], [1])
    apply_tables_json(doc, "{}")
    tables = doc.get("tables", type=Y.Map)
    assert len(tables) == 0


def test_apply_rebuilds_widths_and_cells():
    doc = Y.Doc()
    payload = json.dumps({"t1": {"columns": [200, 320], "rows": [["a", "b"], ["c", "d"]]}})
    apply_tables_json(doc, payload)
    data = json.loads(capture_tables_json(doc))
    assert data == {"t1": {"columns": [200, 320], "rows": [["a", "b"], ["c", "d"]]}}


def test_apply_replaces_previous_state():
    doc = Y.Doc()
    _make_table(doc, "old", [["z"]], [1])
    apply_tables_json(doc, json.dumps({"new": {"columns": [7], "rows": [["q"]]}}))
    tables = doc.get("tables", type=Y.Map)
    assert "old" not in tables
    assert "new" in tables


def test_apply_empty_cells_preserved():
    doc = Y.Doc()
    payload = json.dumps({"t": {"columns": [10, 10], "rows": [["", ""], ["", ""]]}})
    apply_tables_json(doc, payload)
    data = json.loads(capture_tables_json(doc))
    assert data["t"]["rows"] == [["", ""], ["", ""]]


# ── round-trip ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("matrix,widths", [
    ([["a", "b"], ["c", "d"]], [160, 160]),
    ([["h"]], [240]),
    ([["x|y", "z\nw"]], [50, 90]),
    ([["", ""], ["", ""]], [10, 20]),
])
def test_capture_apply_round_trip(matrix, widths):
    doc = Y.Doc()
    _make_table(doc, "rt", matrix, widths)
    captured = capture_tables_json(doc)

    doc2 = Y.Doc()
    apply_tables_json(doc2, captured)
    assert capture_tables_json(doc2) == captured


def test_apply_tables_then_readTableModel_sees_cells():
    """Rebuilt tables must be readable by the existing width-unaware reader too."""
    from table_serialize import table_model_from_map

    doc = Y.Doc()
    apply_tables_json(doc, json.dumps({"t": {"columns": [1, 2], "rows": [["a", "b"]]}}))
    tables = doc.get("tables", type=Y.Map)
    assert table_model_from_map(tables["t"]) == [["a", "b"]]
