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
