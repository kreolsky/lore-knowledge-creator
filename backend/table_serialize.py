"""Table block object ↔ GFM (best-effort, lossy on layout) serializer.

SYSTEM: table-block — backend serializer entry point. The editable table block stores
its model as a Yjs subtree (`ydoc.getMap('tables')`); this module converts the flat
cell-body form (``rows = list[list[str]]``) to a GFM table and back. Used by
``expand_tables`` (derived plaintext / export) and by markdown import.

ARCH (three representations, one source of truth): an editable table exists in THREE
places, by design — future table regressions land here.
  1. ``ydoc_state`` (CRDT) — the authoritative model. ``getMap('tables')`` holds the
     full structure (column widths + cell text) and is what the live widget edits.
  2. ``documents.content`` (GFM) — the DERIVED read-model. ``expand_tables`` splices
     each ``![label](table:id)`` anchor's serialized GFM in at its offset so
     embeddings / search / export see the table text, not an opaque anchor. Lossy:
     column widths and intra-cell layout do not survive (GFM cannot represent them).
  3. ``checkpoints.tables_json`` (JSON) — the LOSSLESS checkpoint capture
     (``capture_tables_json`` / ``apply_tables_json``). ``{ columns, rows }`` per table
     id so a restore rebuilds identical editable blocks. tables_json is the price of
     storing checkpoints as plaintext content (blob) instead of a full ydoc_state
     snapshot: it carries the table state the plaintext anchor alone cannot. (A full
     ydoc_state checkpoint would eliminate representation #3 but is an L rewrite of
     cp_store + the blob dedup contract — out of scope; this is the documented trade-off.)

ARCH: GFM cannot represent per-column widths or intra-cell line breaks. Export is
deliberately lossy — widths drop; a cell ``\\n`` becomes ``<br>`` in the GFM only and is
never written back into the model (parser reverses ``<br>`` → ``\\n``).

INVARIANT: serialize/parse round-trip is lossless for cell TEXT (pipe, newline, empty
cells, significant leading/trailing spaces). Why: the model is the source of truth; an
import (GFM → object) must not corrupt content. Cell padding is exactly ONE space each
side, so the parser strips exactly one — significant spaces survive.
"""
from __future__ import annotations

import json
import re

# Split a GFM row on column separators: a `|` NOT preceded by a backslash escape.
_CELL_SPLIT = re.compile(r"(?<!\\)\|")

# WHY (cross-producer default column width): the px width assigned to a column
# when none is captured. MUST equal the frontend `DEFAULT_COLUMN_WIDTH`
# (frontend/src/components/editor/live-preview/table-block-model.ts). Why: a table
# restored from a checkpoint with missing widths (legacy capture, or a row wider than
# its columns array) renders at this default; a drift between the two producers makes
# the widget render a different width on restore than the editor authored.
# Guarded by table-block-model.test.ts (asserts equality with the frontend constant).
DEFAULT_COLUMN_WIDTH = 160


def _escape_cell(text: str) -> str:
    """Escape a cell body for the GFM wire: literal `|` and intra-cell newlines."""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _unescape_cell(wire: str) -> str:
    """Reverse `_escape_cell`: `<br>` → newline, `\\|` → literal pipe.

    Matches the frontend ``gfm-table-import.ts`` unescape: any ``<br>``/``<br/>``/``<br />``,
    case-insensitive (GFM-spec lenient), so the two parsers agree byte-for-byte.
    """
    return re.sub(r"<br\s*/?>", "\n", wire, flags=re.IGNORECASE).replace("\\|", "|")


def serialize_table(rows: list[list[str]]) -> str:
    """Serialize a table model (cell bodies) to a GFM table string.

    The first row is the header. Ragged rows are padded with empty cells to the
    widest row. Returns ``""`` for an empty model (no rows).
    """
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def _row_line(cells: list[str]) -> str:
        padded = [_escape_cell(c) for c in cells] + [""] * (width - len(cells))
        return "| " + " | ".join(padded) + " |"

    lines = [_row_line(rows[0]), "| " + " | ".join(["---"] * width) + " |"]
    lines.extend(_row_line(r) for r in rows[1:])
    return "\n".join(lines)


def _is_separator(line: str) -> bool:
    """True for a GFM header-separator row (`| --- | :--: | …`)."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return False
    cells = [c.strip() for c in _CELL_SPLIT.split(stripped[1:-1])]
    # Require >=1 NON-EMPTY dash cell. Why: ``all([])`` is True, so without this an all-empty
    # data row (``|   |   |``) is misclassified as the separator and silently dropped on import.
    non_empty = [c for c in cells if c != ""]
    return bool(non_empty) and all(re.fullmatch(r":?-+:?", c) for c in non_empty)


def _parse_row(line: str) -> list[str]:
    inner = line.strip()[1:-1]  # drop leading/trailing pipe
    out: list[str] = []
    for seg in _CELL_SPLIT.split(inner):
        # Strip exactly the one padding space added on each side by serialize_table.
        if seg.startswith(" "):
            seg = seg[1:]
        if seg.endswith(" "):
            seg = seg[:-1]
        out.append(_unescape_cell(seg))
    return out


def table_model_from_map(table) -> list[list[str]]:
    """Extract the flat cell-body matrix from a pycrdt table Map (rows of Y.Text cells).

    Mirror of the frontend ``readTableModel`` rows shape; widths are intentionally not
    read here (GFM drops them). Returns ``[]`` for a structurally broken map.
    """
    rows = table.get("rows")
    if rows is None:
        return []
    return [[str(cell["t"]) for cell in row] for row in rows]


def _table_columns_and_rows(table) -> tuple[list[int], list[list[str]]]:
    """Read BOTH column widths and cell bodies from a pycrdt table Map.

    Width-aware companion to ``table_model_from_map`` (which reads rows only). Capture
    needs widths too — see INVARIANT (shared shape) on ``capture_tables_json``.
    Returns ([], []) for a structurally broken map.
    """
    cols_root = table.get("columns")
    rows_root = table.get("rows")
    if cols_root is None or rows_root is None:
        return [], []
    widths = [int(col["w"]) for col in cols_root]
    rows = [[str(cell["t"]) for cell in row] for row in rows_root]
    return widths, rows


def read_table_grid(doc, table_id: str) -> dict | None:
    """Read one table as `{columns, rows}` (widths + flat cell-body matrix).

    Backend read primitive for the agent `read_table` tool. Returns ``None`` when
    the table id is not present (caller decides 404 vs empty-array semantics).
    """
    from pycrdt import Map

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return None
    widths, rows = _table_columns_and_rows(tables[table_id])
    return {"columns": widths, "rows": rows}


def read_table_cell(doc, table_id: str, row: int, col: int) -> str | None:
    """Read one cell's text, or ``None`` if the table/row/col is out of bounds."""
    from pycrdt import Map

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return None
    rows_root = tables[table_id].get("rows")
    if rows_root is None or row < 0 or row >= len(rows_root):
        return None
    target_row = rows_root[row]
    if col < 0 or col >= len(target_row):
        return None
    return str(target_row[col]["t"])


def set_table_cell(doc, table_id: str, row: int, col: int, text: str) -> bool:
    """Rewrite one cell's `Y.Text` body IN PLACE inside one `doc.transaction()`.

    Backend mirror of the frontend `setCellText` (table-block-model.ts). Returns
    False (no-op, no transaction) when the table/row/col is out of bounds — the
    caller has already validated bounds via `read_table_cell`, so this is a
    defensive re-check, not the primary bounds gate.

    # INVARIANT(corruption): edit the EXISTING `Y.Text` (delete-all + insert), never replace it
    # with a fresh `Text(text)`. Why: a live cell editor is yCollab-bound to that exact
    # Y.Text object; replacing it detaches the bound node and the widget's `render()`
    # short-circuits on unchanged shape (no cell-editor rebuild), so an agent edit stays
    # invisible until a full page reload. In-place edit propagates through the existing
    # binding — matching the frontend `setCellText`.
    """
    from pycrdt import Map

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return False
    rows_root = tables[table_id].get("rows")
    if rows_root is None or row < 0 or row >= len(rows_root):
        return False
    target_row = rows_root[row]
    if col < 0 or col >= len(target_row):
        return False
    body = target_row[col]["t"]
    with doc.transaction():
        if len(body):
            del body[0 : len(body)]
        if text:
            body.insert(0, text)
    return True


def add_table_rows(doc, table_id: str, rows: list[list[str]], width: int) -> None:
    """Append N data rows at the BOTTOM of ``tables[table_id]['rows']`` in ONE
    ``doc.transaction()`` (the backend primitive for the agent ``add_table_rows`` tool).

    Each row is a ``pycrdt.Array`` of ``width`` cell Maps; shorter caller rows pad with
    empty cells (No-silent-degradation: over-length rows are rejected by the caller
    BEFORE this primitive — ``width`` is the LIVE column count). Mirrors frontend
    ``addRow`` (table-block-model.ts) over a pycrdt doc. No-op when ``rows`` is empty.

    # WHY: one transaction per structural op — the broadcast update carries the
    # whole append in one frame, so other replicas converge without an intermediate
    # partial-row state (matches the apply_tables_json rebuild contract).  Why: one transaction per op makes each broadcast frame atomic — replicas apply the whole append without a partial-row intermediate.
    """
    from pycrdt import Array, Map, Text

    if not rows:
        return
    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return
    rows_root = tables[table_id].get("rows")
    if rows_root is None:
        return
    with doc.transaction():
        for row in rows:
            yrow = Array()
            rows_root.append(yrow)
            for i in range(width):
                cell = Map()
                yrow.append(cell)
                cell["t"] = Text(str(row[i]) if i < len(row) else "")


def add_table_column(
    doc, table_id: str, *, at_index: int, header: str | None,
    values: list[str] | None, n_rows: int,
) -> None:
    """Insert ONE column at ``at_index`` into ``tables[table_id]`` in ONE
    ``doc.transaction()`` (the backend primitive for the agent ``add_table_column`` tool).

    Mirrors frontend ``addColumn`` (table-block-model.ts): inserts one
    ``{ w: DEFAULT_COLUMN_WIDTH }`` into ``columns`` and one cell Map into EVERY existing
    row at ``at_index``. Row 0 (the header) gets ``header``; data row ``i`` gets
    ``values[i-1]`` or empty (``values`` is 0-based over DATA rows, so it is offset by the
    header row). ``at_index`` in ``[0, n_cols]`` is validated by the caller.

    # INVARIANT(corruption): columns + every row mutate in ONE transaction so a replica never sees a
    # ragged intermediate (a column without its cells) — the broadcast frame is atomic.  Why: columns and rows mutate in one transaction, so a replica never sees a ragged frame (a new column without its cells).
    """
    from pycrdt import Map, Text

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return
    table = tables[table_id]
    cols_root = table.get("columns")
    rows_root = table.get("rows")
    if cols_root is None or rows_root is None:
        return
    with doc.transaction():
        col = Map()
        cols_root.insert(at_index, col)
        col["w"] = DEFAULT_COLUMN_WIDTH
        for r in range(n_rows):
            row = rows_root[r]
            cell = Map()
            row.insert(at_index, cell)
            if r == 0:
                cell["t"] = Text(header or "")
            else:
                val = values[r - 1] if values is not None and r - 1 < len(values) else ""
                cell["t"] = Text(val)


def create_table_entry(doc, table_id: str, rows_matrix: list[list[str]]) -> None:
    """Build ONE table entry under ``table_id`` inside ``tables`` in ONE
    ``doc.transaction()`` (the backend model-build primitive for the agent
    ``create_table`` tool).

    ``rows_matrix[0]`` is the header (matches frontend ``createTable`` + ``read_table``).
    Width = ``max(len(r) for r in rows_matrix)`` (≥1). Mirrors the per-table build in
    ``apply_tables_json`` (table_serialize.py). The caller splices the
    ``![label](table:id)`` anchor into ``content`` in the SAME transaction (see
    table_writes.apply_create_table) so anchor + model land in one broadcast frame.

    # INVARIANT(corruption): atomic anchor+model — this primitive mutates ONLY the ``tables``
    # subtree; the content anchor splice is the caller's responsibility so BOTH run in
    # one ``doc.transaction()``. Why: a broadcast carrying the model without its anchor
    # (or vice versa) is an intermediate orphan a replica would briefly render as an
    # error (the orphan-anchor/model INVARIANT on table_serialize).
    """
    from pycrdt import Array, Map, Text

    width = max((len(r) for r in rows_matrix), default=1)
    width = max(width, 1)
    tables = doc.get("tables", type=Map)
    with doc.transaction():
        table = Map()
        tables[table_id] = table
        cols = Array()
        table["columns"] = cols
        for _i in range(width):
            c = Map()
            cols.append(c)
            c["w"] = DEFAULT_COLUMN_WIDTH
        rows_root = Array()
        table["rows"] = rows_root
        for row in rows_matrix:
            yrow = Array()
            rows_root.append(yrow)
            for i in range(width):
                cell = Map()
                yrow.append(cell)
                cell["t"] = Text(str(row[i]) if i < len(row) else "")


def capture_tables_json(doc) -> str:
    """Serialize the ``tables`` Yjs subtree to compact JSON for checkpoint storage.

    Output shape (the SHARED contract with the frontend ``serializeTables``):

        { "<tableId>": { "columns": [w0, w1], "rows": [["a","b"],["c","d"]] }, ... }

    ``"{}"`` when the document has no tables. Column entries are px widths (ints), ``rows``
    is the flat cell-body matrix.

    INVARIANT (shared shape, frontend ↔ backend): both producers MUST emit the identical
    JSON shape ``{ columns: number[], rows: string[][] }`` per table. Why: silent drift
    drops column widths on restore (the widget renders a default-width table). The
    frontend ``readTableModel`` already emits ``{ columns, rows }``; this reader mirrors it
    exactly. ``test_table_capture.py`` + ``table-block-model.test.ts`` share one fixture to
    guard byte-identical output across producers.

    Capture is LOSSLESS (unlike ``expand_tables``): cell text is read verbatim, no GFM
    escaping — ``|`` and ``\\n`` survive intact so restore rebuilds identical cells.
    """
    from pycrdt import Map

    tables = doc.get("tables", type=Map)
    if tables is None:
        return "{}"
    out: dict[str, dict] = {}
    for tid, table in tables.items():
        widths, rows = _table_columns_and_rows(table)
        out[str(tid)] = {"columns": widths, "rows": rows}
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def apply_tables_json(doc, tables_json: str | None) -> None:
    """Rebuild the ``tables`` Yjs subtree from ``capture_tables_json`` output.

    Clears ``getMap('tables')`` and rebuilds every table (fresh Yjs structures, NOT a
    replayed update — consistent with the ``SEED_CLIENT_ID`` deterministic-seed rule for
    content in ydoc_store). No-op for ``None`` (legacy restore: leave the tables map
    untouched) and for ``"{}"`` / empty (clears to no tables).

    Runs inside a single ``doc.transaction()`` so the rebuild is one atomic Yjs update — the
    broadcast update (set_content / apply_external_content_change) then carries the whole
    table rebuild in one frame, and other replicas converge without an intermediate
    orphan state. ``restore`` callers MUST hold the session write lock and call this
    BEFORE ``ydoc.get_update()`` so the broadcast includes it.
    """
    from pycrdt import Array, Map, Text

    tables = doc.get("tables", type=Map)
    if tables_json is None:
        # Legacy checkpoint: do not touch the tables map (preserve current behavior).
        return
    data: dict = json.loads(tables_json) if tables_json else {}

    with doc.transaction():
        for key in list(tables.keys()):
            del tables[key]
        for tid, model in data.items():
            cols_src = model.get("columns") or []
            rows_src = model.get("rows") or []
            width = len(cols_src)
            if width == 0 and rows_src:
                width = max(len(r) for r in rows_src)
            # Integrate each container into the doc BEFORE mutating its children — pycrdt
            # raises "Not integrated in a document yet" on __setitem__ of a detached node.
            table = Map()
            tables[tid] = table
            cols = Array()
            table["columns"] = cols
            for w in (cols_src or [DEFAULT_COLUMN_WIDTH] * width):
                c = Map()
                cols.append(c)
                c["w"] = int(w)
            rows = Array()
            table["rows"] = rows
            for row in rows_src:
                yrow = Array()
                rows.append(yrow)
                for i in range(width):
                    cell = Map()
                    yrow.append(cell)
                    cell["t"] = Text(str(row[i]) if i < len(row) else "")



# A table transclusion anchor in the document text: `![label](table:id)`.
_TABLE_ANCHOR = re.compile(r"!\[([^\]]*)\]\(table:([^)]+)\)")


def table_anchor(label: str, table_id: str) -> str:
    """Build a ``![label](table:id)`` transclusion anchor (mirror of the frontend
    ``tableAnchor``). Used by the agent ``create_table`` convergence path to splice a
    fresh table's anchor into the document ``content`` Y.Text."""
    return f"![{label}](table:{table_id})"


def extract_table_labels(content: str) -> dict[str, str]:
    """Map table id -> anchor label for every `![label](table:id)` anchor in content.

    Public helper (read_table pairs a grid to its visible anchor label without
    re-deriving the `_TABLE_ANCHOR` regex in a second module).
    """
    return {m.group(2): m.group(1) for m in _TABLE_ANCHOR.finditer(content)}


def expand_tables(doc) -> str:
    """Resolve every `![label](table:id)` anchor in the doc's `content` to a GFM table.

    see SYSTEM: table-block — the single derived-plaintext expansion pass. Backend consumers
    (embeddings, search, export) read a flat string of the doc; they only see the anchors,
    never the `tables` root. This splices each anchor's serialized model in at its offset.

    INVARIANT: an anchor with NO matching model is left verbatim (best-effort) — never
    dropped. Why: derived content must not silently lose an anchor the user authored; the
    live widget surfaces the orphan as an error instead.
    """
    from pycrdt import Map, Text

    text = str(doc.get("content", type=Text))
    tables = doc.get("tables", type=Map)

    def _repl(m: "re.Match[str]") -> str:
        tid = m.group(2)
        if tid not in tables:
            return m.group(0)
        gfm = serialize_table(table_model_from_map(tables[tid]))
        return gfm if gfm else m.group(0)

    return _TABLE_ANCHOR.sub(_repl, text)


def parse_gfm_table(md: str) -> list[list[str]]:
    """Parse a GFM table string back into the cell-body model (inverse of serialize).

    The separator row is dropped; every other non-empty `|`-delimited line is a data
    row. `<br>` → newline; `\\|` → literal pipe.
    """
    rows: list[list[str]] = []
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        if _is_separator(line):
            continue
        rows.append(_parse_row(line))
    return rows
