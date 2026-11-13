"""`capture_tables_json` / `apply_tables_json` — full table-state capture.

The editable table block stores its model as a Yjs subtree (`getMap('tables')`) SIBLING
to the `content` Text. A checkpoint must capture that subtree completely (table ids,
column widths, cell text) so a restore rebuilds tables identically.

INVARIANT (shared shape, frontend ↔ backend): both producers emit the identical JSON
shape `{ columns: number[], rows: string[][] }` per table. This test pins the backend
producer; `frontend table-serialize.test.ts` pins the frontend producer and uses the
SAME fixture so a silent column-width drop fails loudly.
"""

import pycrdt as Y
from helpers import make_table

from table_serialize import apply_tables_json

# ── apply_tables_json ──────────────────────────────────────────────────────────


def test_apply_none_is_noop():
    doc = Y.Doc()
    make_table(doc, "keep", [["a"]], [99])
    apply_tables_json(doc, None)
    tables = doc.get("tables", type=Y.Map)
    assert "keep" in tables  # untouched on legacy (None) restore


def _merge(doc, table_id: str, anchor: tuple[int, int], end: tuple[int, int]) -> None:
    from helpers import cell_ids

    from table_serialize import cell_key

    table = doc.get("tables", type=Y.Map)[table_id]
    with doc.transaction():
        if "merges" not in table:
            table["merges"] = Y.Map()
        er, ec = cell_ids(doc, table_id, *end)
        table["merges"][cell_key(*cell_ids(doc, table_id, *anchor))] = Y.Map({"r": er, "c": ec})


# TEST-WHY: cross-process-contract — the checkpoint shape is shared with the frontend producer; merges must survive capture → restore and be absent when there are none, so old checkpoints restore unchanged.
def test_capture_restore_keeps_merges_and_omits_empty():
    import json

    from table_serialize import capture_tables_json

    doc = Y.Doc()
    make_table(doc, "t", [["h1", "h2", "h3"], ["a", "", "c"], ["", "", "f"]], [100, 120, 140])
    make_table(doc, "plain", [["x"]])
    _merge(doc, "t", (1, 0), (2, 1))
    captured = capture_tables_json(doc)
    assert json.loads(captured) == {
        "t": {"columns": [100, 120, 140], "rows": [["h1", "h2", "h3"], ["a", "", "c"], ["", "", "f"]],
              "merges": [[1, 0, 2, 1]]},
        "plain": {"columns": [160], "rows": [["x"]]},
    }
    assert list(json.loads(captured)["t"]) == ["columns", "rows", "merges"]

    restored = Y.Doc()
    apply_tables_json(restored, json.dumps({**json.loads(captured), "t": {
        **json.loads(captured)["t"], "merges": [[1, 0, 2, 1], [5, 0, 6, 1]],
    }}))
    assert json.loads(capture_tables_json(restored)) == json.loads(captured)
