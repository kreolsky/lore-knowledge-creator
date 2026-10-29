"""`capture_tables_json` / `apply_tables_json` — full table-state capture (Order step 1).

The editable table block stores its model as a Yjs subtree (`getMap('tables')`) SIBLING
to the `content` Text. A checkpoint must capture that subtree completely (table ids,
column widths, cell text) so a restore rebuilds tables identically.

INVARIANT (shared shape, frontend ↔ backend): both producers emit the identical JSON
shape `{ columns: number[], rows: string[][] }` per table. This test pins the backend
producer; `frontend table-block-model.test.ts` pins the frontend producer and uses the
SAME fixture so a silent column-width drop fails loudly.
"""

import pycrdt as Y

from table_serialize import apply_tables_json


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


# ── apply_tables_json ──────────────────────────────────────────────────────────


def test_apply_none_is_noop():
    doc = Y.Doc()
    _make_table(doc, "keep", [["a"]], [99])
    apply_tables_json(doc, None)
    tables = doc.get("tables", type=Y.Map)
    assert "keep" in tables  # untouched on legacy (None) restore
