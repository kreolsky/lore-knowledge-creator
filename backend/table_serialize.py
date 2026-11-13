"""Table block object → GFM (best-effort, lossy on layout) serializer.

SYSTEM: table-block — backend serializer entry point. The editable table block stores
its model as a Yjs subtree (`ydoc.getMap('tables')`); this module serializes the flat
cell-body form (``rows = list[list[str]]``) to a GFM table; ``expand_tables`` consumes
it for the derived plaintext / export. The inverse GFM→model parse ALSO lives here
(``parse_gfm_table`` + the block scanner ``import_gfm_tables``) — EVERY markdown
import converts through it (the subtree zip, the REST/MCP uploads, the docx/pdf
converter worker, the agent ``import_file``), and so does every agent-AUTHORED
markdown body (create / edit / append — a pipe table the agent writes is born as an
object, never left as text; the collab_writes/apply_edits_resolver/edits.py call
sites reach it through ``convert_import_tables``), and the test oracle
(``tests/backend/helpers.py``) delegates to the same functions, so there is ONE
Python copy. The frontend twin (``gfm-table-import.ts``) stays the sanctioned
UI-side conversion (paste/import in the editor).

ARCH (three representations, one source of truth): an editable table exists in THREE
places, by design — future table regressions land here.
  1. ``ydoc_state`` (CRDT) — the authoritative model. ``getMap('tables')`` holds the
     full structure (column widths + cell text) and is what the live widget edits.
  2. ``documents.content`` (GFM) — the DERIVED read-model. ``expand_tables`` splices
     each ``![label](table:id)`` anchor's serialized GFM in at its offset so
     embeddings / search / export see the table text, not an opaque anchor. Lossy:
     column widths and intra-cell layout do not survive (GFM cannot represent them).
  3. ``checkpoints.tables_json`` (JSON) — the LOSSLESS checkpoint capture
     (``capture_tables_json`` / ``apply_tables_json``). ``{ columns, rows, merges? }`` per table
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
import uuid

from fractional_indexing import generate_key_between, generate_n_keys_between
from pycrdt import Array, Map, Text

# WHY (cross-producer default column width): the px width assigned to a column
# when none is captured. MUST equal the frontend `DEFAULT_COLUMN_WIDTH`
# (frontend/src/components/editor/live-preview/table-widths.ts). Why: a table
# restored from a checkpoint with missing widths (legacy capture, or a row wider than
# its columns array) renders at this default; a drift between the two producers makes
# the widget render a different width on restore than the editor authored.
# Guarded by table-serialize.test.ts (asserts equality with the frontend constant).
DEFAULT_COLUMN_WIDTH = 160
# WHY: the px width a new table is born at. MUST equal the frontend `BIRTH_TABLE_WIDTH`
# (table-widths.ts) and the document text column max width (index.css
# `.cm-content { max-width: 844px }`). Why: an all-px table renders at its sum, so a
# new table fills a full-width document; a drift makes server-born and editor-born
# tables differ in width.
BIRTH_TABLE_WIDTH = 844
MIN_COLUMN_WIDTH = 40
_BIRTH_WEIGHT_MIN = 3
_BIRTH_WEIGHT_MAX = 40


def birth_column_widths(rows_matrix: list[list[str]], total: int = BIRTH_TABLE_WIDTH) -> list[int]:
    """Fixed px widths for a table at birth, proportional to cell text length.

    Weight = code points of the longest trimmed line per cell, clamped to [3, 40].

    # INVARIANT(persisted): TEXT LENGTH only, never font metrics; equals frontend `birthColumnWidths`.
    # Why: server and every client must write the same numbers into the shared model;
    # after birth widths change only by a user's resize.
    """
    width = max(max((len(r) for r in rows_matrix), default=1), 1)
    weights = []
    for col in range(width):
        longest = max(
            (len(line.strip()) for r in rows_matrix if col < len(r)
             for line in str(r[col]).split("\n")),
            default=0,
        )
        weights.append(min(max(longest, _BIRTH_WEIGHT_MIN), _BIRTH_WEIGHT_MAX))
    weight_sum = sum(weights)
    # WHY: floor(x + 0.5), not round() — Python rounds half to even, JS Math.round
    # rounds half up; the twins must agree on every .5.
    px = [max(int(total * w / weight_sum + 0.5), MIN_COLUMN_WIDTH) for w in weights]
    widest = px.index(max(px))
    px[widest] = max(px[widest] + total - sum(px), MIN_COLUMN_WIDTH)
    return px


def freeze_auto_widths(widths: list[int], rows_matrix: list[list[str]]) -> list[int]:
    """Replace every legacy ``0`` ("auto") width with a px width by the birth rule.

    No zero ⇒ unchanged. Every column zero ⇒ ``birth_column_widths`` of the table.
    Mixed ⇒ px columns keep their width; the zero columns share
    ``max(BIRTH_TABLE_WIDTH - sum(px columns), zero_count * MIN_COLUMN_WIDTH)`` by the
    birth rule over their own cells. Mirrors frontend ``freezeAutoWidths``; vectors in
    ``fixtures/table-v2.json`` key ``freeze_auto_widths``.
    """
    zero_idx = [i for i, w in enumerate(widths) if int(w) == 0]
    if not zero_idx:
        return [int(w) for w in widths]
    rows = rows_matrix or [[]]
    sub = [[str(r[i]) if i < len(r) else "" for i in zero_idx] for r in rows]
    px_sum = sum(int(w) for w in widths if int(w) != 0)
    total = max(BIRTH_TABLE_WIDTH - px_sum, len(zero_idx) * MIN_COLUMN_WIDTH)
    born = iter(birth_column_widths(sub, total))
    return [next(born) if int(w) == 0 else int(w) for w in widths]


def _escape_cell(text: str) -> str:
    """Escape a cell body for the GFM wire: literal `|` and intra-cell newlines."""
    return text.replace("|", "\\|").replace("\n", "<br>")


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


# ─── Schema v2: id-keyed rows / columns / cells ──────────────────────────────
#
# ARCH (schema v2): ``tables: Map<tableId, Map{v: 2, columns: Map<colId, Map{pos, w}>,
# rows: Map<rowId, Map{pos}>, cells: Map<"rowId:colId", Text>}>``. Order = sort by
# ``(pos, id)``, plain string comparison, through ``ordered_axis_ids`` — the ONE order
# function on this side (the frontend model owns its twin). Rows and columns are
# keyed, never positional, so a concurrent add-row x add-column merges to a rectangle
# instead of a ragged array.
# ARCH: row/column ``pos`` keys are generated by whoever writes the op — HERE for the
# agent, in the browser for a user — a deliberate exception to ``sort_keys.py``
# ("generation is backend-authoritative"): a CRDT op must apply locally without a
# round-trip. Both sides use the same base-62 fractional-indexing algorithm; the
# shared vector fixture (``test_fractional_key_parity.py``) proves they agree.
# INVARIANT(persisted): inside the system a cell is addressed ONLY by (table_id, row_id, col_id).
# Why: a positional address goes stale the moment a peer inserts a row; an id never does.
# Positions exist at exactly two boundaries — the agent tool entry
# (``agent.table_writes.resolve_cell``) and the whole-matrix projections (GFM in/out,
# ``tables_json`` capture/apply, ``table_model_from_map``).

TABLE_SCHEMA_VERSION = 2


def cell_key(row_id: str, col_id: str) -> str:
    """The ``cells`` map key of one cell."""
    return f"{row_id}:{col_id}"


def _v2_parts(table):
    """``(columns, rows, cells)`` of a v2 table Map, ``None`` for anything else (a v1
    table awaiting migration reads as structurally broken, never half-parsed)."""

    if not isinstance(table, Map) or table.get("v") != TABLE_SCHEMA_VERSION:
        return None
    cols, rows, cells = table.get("columns"), table.get("rows"), table.get("cells")
    if not (isinstance(cols, Map) and isinstance(rows, Map) and isinstance(cells, Map)):
        return None
    return cols, rows, cells


def table_awaits_migration(table) -> bool:
    """True for a schema-v1 table (``columns``/``rows`` are positional ``Array``s) that
    was never rebuilt to v2 (its document deferred before the ``tables_v2`` migration
    was retired).

    # INVARIANT(data-loss): a v1 table is never projected as an empty v2 table — capture
    # skips it, ``apply_tables_json`` keeps it, agent reads report it. Why: an empty
    # projection reaches a checkpoint, and restoring that checkpoint would rebuild the
    # table empty; "awaits migration" is an explicit error, an empty table is a lie.
    """
    return (
        isinstance(table, Map) and table.get("v") is None
        and isinstance(table.get("columns"), Array) and isinstance(table.get("rows"), Array)
    )


def ordered_axis_ids(axis) -> list[str]:
    """Row or column ids of one axis map in display order: sort by ``(pos, id)``.

    # INVARIANT(persisted): the ONE backend order function — every reader (projections,
    # the agent resolver, insert bounds) goes through it. Why: two orderings would show
    # the agent a different table than the user sees, and an edit by row index would
    # land in the wrong row.
    """
    return [i for _, i in sorted((str(m["pos"]), str(i)) for i, m in axis.items())]


def ordered_ids(table) -> tuple[list[str], list[str]]:
    """``(row_ids, col_ids)`` of a table in display order; ``([], [])`` when not v2."""
    parts = _v2_parts(table)
    if parts is None:
        return [], []
    cols, rows, _cells = parts
    return ordered_axis_ids(rows), ordered_axis_ids(cols)


def resolve_merge_entries(row_ids: list[str], col_ids: list[str], merges: dict) -> list[dict]:
    """Accepted merged areas in index space, row-major by anchor: ``[{r0, c0, r1, c1,
    anchor_row_id, anchor_col_id}]``. ``merges`` maps ``cell_key(anchorRow, anchorCol)``
    to ``{"r": endRowId, "c": endColId}`` (the BOTTOM-RIGHT cell, by id).

    An entry is accepted only if anchor and end ids exist, the end is not above/left of
    the anchor, the area is >= 2 cells, it does not span the header row and body rows at
    once, and it does not overlap an earlier accepted area; a rejected entry reads as
    plain cells. Equals frontend ``resolveMerges`` (parity fixture ``table-v2.json`` →
    ``merges``).

    # INVARIANT(persisted): resolve on read, never repair — the model is not touched.
    # Why: a concurrent removal or merge can leave an entry that no longer describes an
    # area; repairing it on one replica would fork the CRDT, and two resolvers that
    # disagree show the agent a different table than the user sees.
    """
    row_at = {rid: i for i, rid in enumerate(row_ids)}
    col_at = {cid: i for i, cid in enumerate(col_ids)}
    anchors = {cell_key(r, c): (ri, ci) for r, ri in row_at.items() for c, ci in col_at.items()}
    candidates = []
    for key, end in merges.items():
        if key not in anchors or not hasattr(end, "get"):
            continue
        r1, c1 = row_at.get(str(end.get("r"))), col_at.get(str(end.get("c")))
        if r1 is None or c1 is None:
            continue
        r0, c0 = anchors[key]
        candidates.append((r0, c0, r1, c1))
    accepted: list[dict] = []
    for r0, c0, r1, c1 in sorted(candidates):
        if r1 < r0 or c1 < c0 or (r1 - r0 + 1) * (c1 - c0 + 1) < 2:
            continue
        if r0 == 0 < r1:
            continue
        if any(r0 <= a["r1"] and a["r0"] <= r1 and c0 <= a["c1"] and a["c0"] <= c1 for a in accepted):
            continue
        accepted.append({
            "r0": r0, "c0": c0, "r1": r1, "c1": c1,
            "anchor_row_id": row_ids[r0], "anchor_col_id": col_ids[c0],
        })
    return accepted


def resolve_merges(table) -> list[dict]:
    """Accepted merged areas of a table Map (see ``resolve_merge_entries``); ``[]`` when
    the table is not v2 or carries no ``merges`` map."""
    parts = _v2_parts(table)
    merges = table.get("merges") if parts is not None else None
    if not isinstance(merges, Map):
        return []
    row_ids, col_ids = ordered_ids(table)
    return resolve_merge_entries(row_ids, col_ids, {str(k): v for k, v in merges.items()})


def _pos_between(axis, before_id: str | None, after_id: str | None) -> str:
    """A ``pos`` key for an item placed right after ``before_id`` and right before
    ``after_id`` (either may be ``None`` = that end of the axis).

    WHY: equal ``pos`` values are legal (two replicas inserting at the same gap mint
    the same key); ``generate_key_between`` raises on ``a >= b``, so the upper bound
    is then taken from the next DISTINCT ``pos`` after the lower one.
    """

    lo = str(axis[before_id]["pos"]) if before_id is not None else None
    hi = str(axis[after_id]["pos"]) if after_id is not None else None
    if lo is not None and hi is not None and lo >= hi:
        hi = next((p for p in sorted(str(m["pos"]) for m in axis.values()) if p > lo), None)
    return generate_key_between(lo, hi)


def _last_pos(axis) -> str | None:
    """The greatest ``pos`` on an axis, ``None`` when empty."""
    return max((str(m["pos"]) for m in axis.values()), default=None)


def _new_id() -> str:
    return str(uuid.uuid4())


def _build_table(tables, table_id: str, widths: list[int], matrix: list[list[str]],
                 merges: list | None = None) -> None:
    """Write one fresh v2 table under ``table_id``: ``len(widths)`` columns, one row
    per matrix row, a cell per row x column (EAGER). ``merges`` (checkpoint shape,
    ``[[r0, c0, r1, c1], …]``) become id-keyed entries; one whose indices fall outside
    the grid is skipped. Caller owns the transaction."""

    col_ids = [_new_id() for _ in widths]
    cols = {
        cid: Map({"pos": pos, "w": int(w)})
        for cid, pos, w in zip(col_ids, generate_n_keys_between(None, None, len(widths)), widths)
    }
    rows: dict = {}
    cells: dict = {}
    for row, pos in zip(matrix, generate_n_keys_between(None, None, len(matrix))):
        rid = _new_id()
        rows[rid] = Map({"pos": pos})
        for i, cid in enumerate(col_ids):
            cells[cell_key(rid, cid)] = Text(str(row[i]) if i < len(row) else "")
    row_ids = list(rows)
    model = {"v": TABLE_SCHEMA_VERSION, "columns": Map(cols), "rows": Map(rows), "cells": Map(cells)}
    entries = {
        cell_key(row_ids[r0], col_ids[c0]): Map({"r": row_ids[r1], "c": col_ids[c1]})
        for r0, c0, r1, c1 in (m for m in merges or [] if _merge_in_grid(m, len(row_ids), len(col_ids)))
    }
    if entries:
        model["merges"] = Map(entries)
    tables[table_id] = Map(model)


def _merge_in_grid(entry, n_rows: int, n_cols: int) -> bool:
    """A checkpoint ``[r0, c0, r1, c1]`` whose four indices address the restored grid."""
    return (
        isinstance(entry, list) and len(entry) == 4 and all(isinstance(i, int) for i in entry)
        and 0 <= entry[0] < n_rows and 0 <= entry[2] < n_rows
        and 0 <= entry[1] < n_cols and 0 <= entry[3] < n_cols
    )


def _table_columns_and_rows(table) -> tuple[list[int], list[list[str]]]:
    """Whole-matrix projection: column widths + the cell-body matrix, in display order.

    A missing cell (the concurrent add-row x add-column intersection) reads empty;
    orphan cells (a row or column removed concurrently) are never read. Returns
    ``([], [])`` for a table that is not v2.
    """
    parts = _v2_parts(table)
    if parts is None:
        return [], []
    cols, rows, cells = parts
    col_ids = ordered_axis_ids(cols)
    widths = [int(cols[cid]["w"]) for cid in col_ids]
    matrix = [
        [str(cells[k]) if (k := cell_key(rid, cid)) in cells else "" for cid in col_ids]
        for rid in ordered_axis_ids(rows)
    ]
    return widths, matrix


def table_model_from_map(table) -> list[list[str]]:
    """Whole-matrix projection: the flat cell-body matrix of a table, display order.

    Mirror of the frontend ``readTableModel`` rows shape; widths are intentionally not
    read here (GFM drops them). Returns ``[]`` for a structurally broken map.
    """
    return _table_columns_and_rows(table)[1]


def _table(doc, table_id: str):

    tables = doc.get("tables", type=Map)
    if tables is None or table_id not in tables:
        return None
    return _v2_parts(tables[table_id])


def read_table_cell(doc, table_id: str, row_id: str, col_id: str) -> str | None:
    """Read one cell's text; ``None`` when the table, row or column does not exist.
    A missing cell of a live row x column reads ``""``."""
    parts = _table(doc, table_id)
    if parts is None:
        return None
    cols, rows, cells = parts
    if row_id not in rows or col_id not in cols:
        return None
    key = cell_key(row_id, col_id)
    return str(cells[key]) if key in cells else ""


def set_table_cell(doc, table_id: str, row_id: str, col_id: str, text: str) -> bool:
    """Rewrite one cell's ``Y.Text`` body IN PLACE inside one ``doc.transaction()``.

    Returns False (no-op, no transaction) when the table, row or column does not exist — the
    caller has already validated via ``read_table_cell``, so this is a defensive
    re-check, not the primary gate. A missing cell of a live row x column is created
    by this first write.

    # INVARIANT(corruption): edit the EXISTING `Y.Text` (delete-all + insert), never replace it
    # with a fresh `Text(text)`. Why: a live cell editor is yCollab-bound to that exact
    # Y.Text object; replacing it detaches the bound node and the widget's `render()`
    # short-circuits on unchanged shape (no cell-editor rebuild), so an agent edit stays
    # invisible until a full page reload. In-place edit propagates through the existing
    # binding.
    """

    parts = _table(doc, table_id)
    if parts is None:
        return False
    cols, rows, cells = parts
    if row_id not in rows or col_id not in cols:
        return False
    key = cell_key(row_id, col_id)
    with doc.transaction():
        if key not in cells:
            cells[key] = Text(text)
            return True
        body = cells[key]
        if len(body):
            del body[0 : len(body)]
        if text:
            body.insert(0, text)
    return True


def add_table_rows(doc, table_id: str, rows: list[dict[str, str]]) -> list[str]:
    """Append N rows at the BOTTOM of a table in ONE ``doc.transaction()`` (the backend
    primitive for the agent ``add_table_rows`` tool). Each row maps ``col_id`` → text.

    Cells are EAGER: every row gets a cell per LIVE column; a column absent from the
    row dict reads empty, a key naming no live column is dropped (an orphan cell is
    never written). Returns the new row ids in order; ``[]`` (no transaction) when
    ``rows`` is empty or the table does not exist. Mirrors frontend ``addRow``.

    # WHY: one transaction per structural op — the broadcast update carries the
    # whole append in one frame, so other replicas converge without an intermediate
    # partial-row state (matches the apply_tables_json rebuild contract).
    """

    parts = _table(doc, table_id)
    if not rows or parts is None:
        return []
    cols, rows_root, cells = parts
    col_ids = ordered_axis_ids(cols)
    row_ids = [_new_id() for _ in rows]
    with doc.transaction():
        for rid, pos, row in zip(row_ids, generate_n_keys_between(_last_pos(rows_root), None, len(rows)), rows):
            rows_root[rid] = Map({"pos": pos})
            for cid in col_ids:
                cells[cell_key(rid, cid)] = Text(str(row.get(cid, "")))
    return row_ids


def add_table_column(
    doc, table_id: str, *, before_id: str | None, after_id: str | None,
    texts: dict[str, str],
) -> str | None:
    """Insert ONE column between ``before_id`` (the column that ends up right BEFORE
    it, ``None`` = first) and ``after_id`` (right AFTER it, ``None`` = last) in ONE
    ``doc.transaction()`` (the backend primitive for the agent ``add_table_column``
    tool). ``texts`` maps ``row_id`` → the new cell's text; every live row gets a cell.
    Returns the new column id, ``None`` when the table does not exist.

    Width: the ``before_id`` neighbour's (the agent has no cursor column to inherit
    from), ``DEFAULT_COLUMN_WIDTH`` when inserted first.

    # INVARIANT(persisted): every column always has a fixed px width (>= MIN_COLUMN_WIDTH);
    # there are no auto columns.
    # Why: table widths must be predictable and never reflow by themselves.

    # INVARIANT(corruption): the column + a cell for every row mutate in ONE transaction so
    # a replica never sees a column without its cells. Why: the broadcast frame is atomic.
    """

    parts = _table(doc, table_id)
    if parts is None:
        return None
    cols, rows, cells = parts
    width = int(cols[before_id]["w"]) if before_id is not None and before_id in cols else DEFAULT_COLUMN_WIDTH
    col_id = _new_id()
    with doc.transaction():
        cols[col_id] = Map({
            "pos": _pos_between(cols, before_id, after_id),
            "w": max(width, MIN_COLUMN_WIDTH),
        })
        for rid in rows.keys():
            cells[cell_key(rid, col_id)] = Text(str(texts.get(rid, "")))
    return col_id


def create_table_entry(doc, table_id: str, rows_matrix: list[list[str]]) -> None:
    """Build ONE table entry under ``table_id`` inside ``tables`` in ONE
    ``doc.transaction()`` (the backend model-build primitive for the agent
    ``create_table`` tool) — a whole-matrix projection, ``rows_matrix[0]`` is the
    header (matches frontend ``createTable`` + ``read_table``).

    Width = ``max(len(r) for r in rows_matrix)`` (>=1). The caller splices the
    ``![label](table:id)`` anchor into ``content`` in the SAME transaction (see
    table_writes.apply_create_table) so anchor + model land in one broadcast frame.

    # INVARIANT(corruption): atomic anchor+model — this primitive mutates ONLY the ``tables``
    # subtree; the content anchor splice is the caller's responsibility so BOTH run in
    # one ``doc.transaction()``. Why: a broadcast carrying the model without its anchor
    # (or vice versa) is an intermediate orphan a replica would briefly render as an
    # error (the orphan-anchor/model INVARIANT on table_serialize).
    """

    tables = doc.get("tables", type=Map)
    with doc.transaction():
        _build_table(tables, table_id, birth_column_widths(rows_matrix), rows_matrix)


def capture_tables_json(doc) -> str:
    """Serialize the ``tables`` Yjs subtree to compact JSON for checkpoint storage.

    Output shape (the SHARED contract with the frontend ``serializeTables``) — POSITIONAL,
    rows and columns in display order, ids dropped:

        { "<tableId>": { "columns": [w0, w1], "rows": [["a","b"],["c","d"]],
                         "merges": [[r0, c0, r1, c1]] }, ... }

    ``"{}"`` when the document has no tables. Column entries are px widths (ints), ``rows``
    is the flat cell-body matrix, ``merges`` the accepted merged areas in row-major anchor
    order — emitted ONLY when non-empty (absent = no merges, so old checkpoints restore
    unchanged).

    INVARIANT (shared shape, frontend ↔ backend): both producers MUST emit the identical
    JSON shape ``{ columns: number[], rows: string[][], merges?: number[][] }`` per table,
    key order ``columns, rows, merges``. Why: silent drift
    drops column widths on restore (the widget renders a default-width table). The
    frontend ``readTableModel`` already emits ``{ columns, rows }``; this reader mirrors it
    exactly. ``test_table_capture.py`` + ``table-serialize.test.ts`` share one fixture to
    guard byte-identical output across producers.

    Capture is LOSSLESS (unlike ``expand_tables``): cell text is read verbatim, no GFM
    escaping — ``|`` and ``\\n`` survive intact so restore rebuilds identical cells.
    A v1 table awaiting migration is OMITTED, never emitted empty.
    """

    tables = doc.get("tables", type=Map)
    if tables is None:
        return "{}"
    out: dict[str, dict] = {}
    for tid, table in tables.items():
        if table_awaits_migration(table):
            continue  # see INVARIANT on table_awaits_migration
        widths, rows = _table_columns_and_rows(table)
        entry: dict = {"columns": widths, "rows": rows}
        if merges := [[a["r0"], a["c0"], a["r1"], a["c1"]] for a in resolve_merges(table)]:
            entry["merges"] = merges
        out[str(tid)] = entry
    return json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def apply_tables_json(doc, tables_json: str | None) -> None:
    """Rebuild the ``tables`` Yjs subtree from ``capture_tables_json`` output.

    Clears ``getMap('tables')`` and rebuilds every table as v2 with FRESH row/column ids
    (fresh Yjs structures, NOT a replayed update — consistent with the
    ``SEED_CLIENT_ID`` deterministic-seed rule for content in ydoc_store). No-op for
    ``None`` (legacy restore: leave the tables map untouched) and for ``"{}"`` / empty
    (clears to no tables). A v1 table the snapshot does not name is KEPT — capture
    skipped it (see ``table_awaits_migration``), so the snapshot holds no truth about it.

    Runs inside a single ``doc.transaction()`` so the rebuild is one atomic Yjs update — the
    broadcast update (set_content / apply_external_content_change) then carries the whole
    table rebuild in one frame, and other replicas converge without an intermediate
    orphan state. ``restore`` callers MUST hold the session write lock and call this
    BEFORE ``ydoc.get_update()`` so the broadcast includes it.
    """

    tables = doc.get("tables", type=Map)
    if tables_json is None:
        # Legacy checkpoint: do not touch the tables map (preserve current behavior).
        return
    data: dict = json.loads(tables_json) if tables_json else {}

    with doc.transaction():
        for key in list(tables.keys()):
            if key in data or not table_awaits_migration(tables[key]):
                del tables[key]
        for tid, model in data.items():
            cols_src = model.get("columns") or []
            rows_src = model.get("rows") or []
            width = len(cols_src)
            if width == 0 and rows_src:
                width = max(len(r) for r in rows_src)
            # WHY: an old checkpoint may hold legacy 0 widths; a restore never brings one back.
            widths = freeze_auto_widths(cols_src or [DEFAULT_COLUMN_WIDTH] * width, rows_src)
            _build_table(tables, tid, widths, rows_src, model.get("merges"))


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

    text = str(doc.get("content", type=Text))
    tables = doc.get("tables", type=Map)

    def _repl(m: "re.Match[str]") -> str:
        tid = m.group(2)
        if tid not in tables:
            return m.group(0)
        gfm = serialize_table(table_model_from_map(tables[tid]))
        return gfm if gfm else m.group(0)

    return _TABLE_ANCHOR.sub(_repl, text)


# ─── GFM → model (the inverse direction; shared with the test oracle) ────────

_CELL_SPLIT = re.compile(r"(?<!\\)\|")

# An image-embed span (`![alt|WxH](ref:id)`) — its `|` is the SIZE SEPARATOR of
# the sized-image shape (subtree_md._rewrite_embeds emits exactly this form),
# never a cell delimiter. Masked before a row split so an embed never
# fragments its cell.
_IMAGE_EMBED = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _split_row(line: str) -> list[str]:
    """Split one GFM table row into raw cell segments (padding + escapes kept).

    Drops ONE optional leading ``|`` and ONE optional unescaped trailing ``|``
    (outer pipes are optional in GFM), then splits on unescaped pipes OUTSIDE
    image-embed spans. The ONE splitter — ``_is_separator``, ``_parse_row`` and
    ``parse_gfm_table`` ride it, and the frontend twin
    (``gfm-table-import.ts``) must agree byte-for-byte; the shared fixture in
    ``test_table_serialize.py`` / ``gfm-table-import.test.ts`` enforces it.

    Why the trailing drop is escape-aware: ``| a | b\\|`` ends in an ESCAPED
    pipe (cell content), not the row delimiter. Embeds are masked behind a
    ``\\x00`` placeholder (never a literal in text content) and restored per
    segment, in order.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    embeds = [m.group(0) for m in _IMAGE_EMBED.finditer(s)]
    if not embeds:
        return _CELL_SPLIT.split(s)
    masked = _IMAGE_EMBED.sub("\x00", s)
    restore = iter(embeds)
    return [
        re.sub("\x00", lambda _m: next(restore), seg)
        for seg in _CELL_SPLIT.split(masked)
    ]


def _unescape_cell(wire: str) -> str:
    """Reverse `_escape_cell`: `<br>` → newline, `\\|` → literal pipe.

    Matches the frontend ``gfm-table-import.ts`` unescape: any ``<br>``/``<br/>``/
    ``<br />``, case-insensitive (GFM-spec lenient), so the two parsers agree
    byte-for-byte."""
    return re.sub(r"<br\s*/?>", "\n", wire, flags=re.IGNORECASE).replace("\\|", "|")


def _is_separator(line: str) -> bool:
    """True for a GFM header-separator row (`| --- | :--: | …`, outer pipes
    optional). NOTE: a bare setext/thematic run (``---``) is also dash-only —
    the HEADER rules in ``_collect_table_block`` keep setext headings out of a
    conversion (a no-leading-pipe header requires a separator WITH a pipe and
    an equal cell count), not this predicate."""
    cells = [c.strip() for c in _split_row(line)]
    # Require >=1 NON-EMPTY dash cell. Why: ``all([])`` is True, so without this an
    # all-empty data row (``|   |   |``) is misclassified as the separator and
    # silently dropped on import.
    non_empty = [c for c in cells if c != ""]
    return bool(non_empty) and all(re.fullmatch(r":?-+:?", c) for c in non_empty)


def _parse_row(line: str) -> list[str]:
    out: list[str] = []
    for seg in _split_row(line):
        # Strip exactly the one padding space added on each side by serialize_table.
        if seg.startswith(" "):
            seg = seg[1:]
        if seg.endswith(" "):
            seg = seg[:-1]
        out.append(_unescape_cell(seg))
    return out


def parse_gfm_table(md: str) -> list[list[str]]:
    """Parse a GFM table string into the cell-body model (the inverse of
    `serialize_table`). The separator row is dropped; every other non-empty
    pipe-delimited line (outer pipes optional) is a data row. `<br>` → newline;
    `\\|` → literal pipe."""
    rows: list[list[str]] = []
    for line in md.splitlines():
        s = line.strip()
        if not s:
            continue
        # A data row is pipe-delimited: outer pipes OR an inner unescaped pipe —
        # plain prose lines never parse as single-cell rows.
        if not s.startswith("|") and len(_split_row(line)) < 2:
            continue
        if _is_separator(line):
            continue
        rows.append(_parse_row(line))
    return rows


def import_gfm_tables(markdown: str, label: str = "Table") -> tuple[str, str]:
    """Replace every GFM table BLOCK in `markdown` with a fresh table anchor,
    building the models — the backend mirror of the frontend
    ``importGfmTables`` (the sanctioned UI-side conversion; this is the
    file-import side). Returns ``(rewritten_text, tables_json)`` where
    tables_json is the ``capture_tables_json`` shape, ready for
    ``ydoc_store.set_content(..., tables_json=...)``.

    A block = a pipe-line header immediately followed by a separator row, then
    the body run (outer pipes optional — see ``_collect_table_block``). Fenced
    code is skipped verbatim (``fence_mask``): a pipe table inside a fence is
    code, never a table. Prose (and pipes not forming a table) are left
    untouched.
    """
    from markdown_normalize import fence_mask

    lines = markdown.split("\n")
    mask = fence_mask(lines)
    out: list[str] = []
    tables: dict[str, dict] = {}
    i = 0
    while i < len(lines):
        matrix, next_i = _collect_table_block(lines, i, mask)
        if matrix is not None:
            table_id = uuid.uuid4().hex
            tables[table_id] = {
                "columns": birth_column_widths(matrix),
                "rows": matrix,
            }
            out.append(table_anchor(label, table_id))
            i = next_i
            continue
        out.append(lines[i])
        i += 1

    return "\n".join(out), json.dumps(tables, separators=(",", ":"),
                                      ensure_ascii=False)


def _row_has_pipe(line: str) -> bool:
    """The line holds a cell-delimiter ``|`` outside image-embed spans (an
    embed's ``|`` is the sized-image separator, not a cell delimiter)."""
    return bool(_CELL_SPLIT.search(_IMAGE_EMBED.sub("\x00", line)))


def _separator_matches_header(
    lines: list[str], i: int, mask: list[bool], has_leading_pipe: bool,
) -> bool:
    """The delimiter row under the header at `i`, shaped for its form:
    ``_is_separator`` first, then — a pipe-PREFIXED separator for a
    leading-pipe header (a bare ``---`` under a pipe header stays
    thematic/setext); for a no-pipe header, a separator HOLDING a pipe with an
    EQUAL cell count (``a\\n---`` and ``a | b\\n---`` stay setext headings)."""
    if i + 1 >= len(lines) or mask[i + 1] or not _is_separator(lines[i + 1]):
        return False
    if has_leading_pipe:
        return lines[i + 1].strip().startswith("|")
    sep = lines[i + 1]
    return _row_has_pipe(sep) and len(_split_row(lines[i])) == len(_split_row(sep))


def _block_end(lines: list[str], i: int, mask: list[bool],
               has_leading_pipe: bool) -> int:
    """The index of the block's LAST line from header `i`: leading-pipe bodies
    run over pipe-prefixed lines; no-outer-pipe bodies over NON-BLANK lines
    holding an unescaped pipe. Fenced lines (``mask``) never continue a block."""
    if has_leading_pipe:
        def _body(idx: int) -> bool:
            return lines[idx].strip().startswith("|")
    else:
        def _body(idx: int) -> bool:
            body = lines[idx]
            return bool(body.strip()) and _row_has_pipe(body)
    j = i + 1
    while j + 1 < len(lines) and not mask[j + 1] and _body(j + 1):
        j += 1
    return j


def _collect_table_block(
    lines: list[str], i: int, mask: list[bool],
) -> tuple[list | None, int]:
    """From line `i`, collect one GFM table block's matrix (header + data
    rows, separator dropped) → (matrix, next_index_after_block); (None, i)
    when line i does not START a table.

    Two header shapes:
      · leading-pipe (``| a | b |``) + a pipe-prefixed separator — the classic
        form; the body runs over pipe-prefixed lines;
      · no outer pipes (``a | b``) + a pipe-holding separator with an equal
        cell count — the body runs over non-blank lines holding an unescaped
        pipe. Blockquoted tables stay text (the ``> `` prefix never parses as
        cells). Fenced lines (``mask``) never start or continue a block — a
        converted table would land its anchor INSIDE the fence. A block nested
        in a list item converts and its anchor lands at top level (the block's
        indented lines are replaced by one unindented anchor line).
    """
    if mask[i]:
        return None, i
    line = lines[i]
    if not line.strip() or _is_separator(line):
        return None, i
    has_leading_pipe = line.strip().startswith("|")
    if not has_leading_pipe and not _row_has_pipe(line):
        return None, i
    if not _separator_matches_header(lines, i, mask, has_leading_pipe):
        return None, i
    j = _block_end(lines, i, mask, has_leading_pipe)
    matrix = [
        _parse_row(l) for l in lines[i:j + 1] if not _is_separator(l)
    ]
    return (matrix if matrix else None), j + 1
