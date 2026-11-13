/**
 * Table-block model — read/write helpers for the `tables` Yjs subtree (schema v2).
 *
 * SYSTEM: table-block — frontend model entry point. The editable table object stores its
 * data OUTSIDE the plain-text buffer, as a sibling root in the same entity Y.Doc:
 *
 *     ydoc.getMap('tables') : Y.Map<tableId, Y.Map{
 *       v: 2,
 *       label?: string,
 *       columns: Y.Map<colId, Y.Map{ pos: string, w: number }>,   // px width, always >= MIN_COLUMN_WIDTH
 *       rows:    Y.Map<rowId, Y.Map{ pos: string }>,
 *       cells:   Y.Map<"rowId:colId", Y.Text>,                    // cell body
 *       merges?: Y.Map<"anchorRowId:anchorColId", Y.Map{ r: rowId, c: colId }>,
 *     }>
 *
 * ARCH (merged cells): a `merges` entry is keyed by the area's top-left cell and names its
 * BOTTOM-RIGHT cell by id, never a span count — a count is positional (see the
 * INVARIANT(persisted) below). A row/column inserted strictly inside the area extends it by
 * itself. Covered cells stay in `cells` (the grid stays rectangular) and are emptied by
 * the merge; text a race leaves in one is hidden in the editor, never repaired. Entries are
 * resolved on read (`resolveMerges`), never repaired.
 *
 * The document `content` text holds only a transclusion anchor `![label](table:id)`.
 *
 * ARCH (schema v2, id-keyed): rows and columns are keyed maps ordered by `(pos, id)`
 * (plain string comparison) — see `orderedAxisIds`, the ONE order function on this side
 * (the backend twin is `table_serialize.ordered_axis_ids`). Rows/columns are never
 * positional, so a concurrent add-row × add-column merges to a rectangle instead of a
 * ragged array, and a peer inserting a row never invalidates an edit's address.
 *
 * ARCH: row/column `pos` keys are generated ON THE CLIENT (whoever writes the op) —
 * a deliberate exception to backend-authoritative sort-key generation: a CRDT op must
 * apply locally without a round-trip. Both sides use the same base-62 algorithm
 * (npm `fractional-indexing`, pinned EXACTLY `3.2.0`); the shared vector fixture
 * (`fixtures/table-v2.json`, read by this tree's vector test and by the backend
 * `test_fractional_key_parity.py`) proves the two libraries agree key for key.
 *
 * ARCH: cell body is a per-cell Y.Text so concurrent edits to different cells (and width
 * drags) never conflict and never produce an invalid intermediate state — the reason the
 * model is a CRDT subtree, not a JSON string in the text buffer.
 *
 * INVARIANT(persisted): inside the system a cell is addressed ONLY by
 * `(tableId, rowId, colId)`. Why: a positional address goes stale the moment a peer
 * inserts a row; an id never does. Model functions, the widget's DOM (`data-row-id`/
 * `data-col-id`), find marks, Replace-all, badges and undo meta all address by ids;
 * a position (row/column index) exists at exactly TWO boundaries — the whole-matrix
 * projections kept here (`readTableModel`, `serializeTables`/`applyTablesJson`,
 * `modelToGfm`, GFM import; a full ordered copy, not addressing) and the agent tool
 * entry on the backend.
 *
 * INVARIANT: these helpers are the ONLY sanctioned write path; the widget is a pure
 * reflection of this model (Thin Client). Why: keeps collab merge correct and the widget
 * stateless. Every mutation runs inside a single `doc.transact` so one widget action = one
 * atomic Yjs update.
 *
 * INVARIANT(data-loss): a table removed WHOLE from the document text drops its model too.
 * Why: a one-key delete is refused (atomic-delete-guard), so removing a whole anchor is a
 * deliberate act (see SYSTEM: table-block, table-reconcile.ts). An orphan MODEL left any
 * other way (a corrupted `table:id`, an agent edit) is TOLERATED — retained, never
 * auto-pruned, listed unlinked in the References tab — because a corrupted anchor is
 * indistinguishable from a removal by "no matching anchor", and auto-cleanup keyed on that
 * lost whole tables on one mangled char. Only an orphan ANCHOR (anchor present, no model)
 * renders an inline error.
 */
import * as Y from 'yjs';
import { generateNKeysBetween } from 'fractional-indexing';
import { uuid } from '../../../utils/uuid';
import { stopCapturing } from '../../../collab/document-undo';
import { BIRTH_TABLE_WIDTH, DEFAULT_COLUMN_WIDTH, MIN_COLUMN_WIDTH, birthColumnWidths } from './table-widths';
import { appendPos, cachedOrder, posBetween } from './table-order';

/** The table subtree schema this code reads and writes. MUST equal the backend
 * `TABLE_SCHEMA_VERSION` — a server speaking a different version is kept out by the
 * collab protocol gate (`?proto=`, see yjs-provider.ts), not by this constant. */
export const TABLE_SCHEMA_VERSION = 2;

/**
 * Yjs transaction origins for every model write — the undo-stack contract (the
 * document-wide `Y.UndoManager` tracks `TABLE_LOCAL_ORIGIN`, never the system one):
 *
 *  - `TABLE_LOCAL_ORIGIN` — user-initiated writes (insert/import a table, rename, delete,
 *    structural ops, cell rewrites). One user action = one undoable step.
 *  - `TABLE_SYSTEM_ORIGIN` — deliberately OUTSIDE the undo stack: `applyTablesJson`
 *    seeding (PublicEditor / snapshot preview / useDocumentTables) and
 *    `cloneDuplicateAnchors` reconciliation. Why: seeding replays a whole captured
 *    state — undoing it would "un-insert" a table the user just navigated to, and the
 *    reconcile clone is a background repair, not a user action.
 *
 * WHY explicit origins at all: the Yjs default captures every origin-less local write
 * (`new Set([null])`), so a model without origins would land random internal writes in
 * the undo stack. Nested `doc.transact` calls JOIN the outermost transaction — its origin
 * wins — so a public helper called inside a system transaction (e.g. `cloneTable` from
 * `cloneDuplicateAnchors`) inherits the system origin without extra plumbing.
 */
export const TABLE_LOCAL_ORIGIN = Symbol('table-local');
export const TABLE_SYSTEM_ORIGIN = Symbol('table-system');

/**
 * One user-initiated structural/width write = ONE undo step: the document-wide stack is
 * closed before AND after it, so text typed around it within the capture timeout never
 * merges into it. Inside an open undo group (a drag) the boundaries are suppressed and
 * the whole gesture stays one step (see collab/document-undo.ts).
 * WHY creation and `deleteTable` stay plain `transact`: their callers pair them with the
 * anchor text edit (insert / remove), which must land in the SAME step.
 */
export function localStep(doc: Y.Doc, fn: () => void): void {
  stopCapturing(doc);
  doc.transact(fn, TABLE_LOCAL_ORIGIN);
  stopCapturing(doc);
}

/**
 * A row id as minted by this model (uuid via `uuid()` locally, or read back from the
 * CRDT). Branded so a positional index can never silently pass as one — the only mint is
 * the model (create/add paths and the order functions).
 */
declare const rowIdBrand: unique symbol;
export type RowId = string & { readonly [rowIdBrand]: never };
/** A column id — see `RowId`. */
declare const colIdBrand: unique symbol;
export type ColId = string & { readonly [colIdBrand]: never };

/** Whole-matrix projection: a table as flat `{columns, rows}` (display order). The
 * read-only shape for consumers that copy/serialize the WHOLE table — never an
 * addressing scheme (see the INVARIANT(persisted) in the header). */
export interface TableModel {
  columns: number[];
  rows: string[][];
}

/** One column's width in display order — `readColumnWidths`/`setColumnWidths` items. */
export interface ColumnWidth {
  id: ColId;
  w: number;
}

/** Internal to the table-* modules. */
export type AxisEntry = Y.Map<string | number>; // { pos } on rows, { pos, w } on columns
type CellText = Y.Text;
/** A `tables` entry (v2 or legacy v1 — see `tableAwaitsMigration`). Internal to the table-* modules. */
export type TableMap = Y.Map<unknown>;

export function getTablesMap(doc: Y.Doc): Y.Map<TableMap> {
  return doc.getMap('tables') as Y.Map<TableMap>;
}

/** The `cells` map key of one cell — `"<rowId>:<colId>"`. Ids are uuids (no `:`), so the
 * split is unambiguous in both directions. */
export function cellKey(rowId: RowId, colId: ColId): string {
  return `${rowId}:${colId}`;
}

/**
 * True for a schema-v1 table (positional `columns`/`rows` Y.Arrays) that was never
 * rebuilt to v2 (its document deferred before the `tables_v2` backend migration was retired).
 *
 * INVARIANT(data-loss): a v1 table is never projected as an empty v2 table —
 * `serializeTables` skips it, `applyTablesJson` keeps it, and the widget renders
 * "table awaits migration". Why: an empty projection reaches a checkpoint, and restoring
 * that checkpoint would rebuild the table empty; "awaits migration" is an explicit
 * error, an empty table is a lie. Backend twin: `table_serialize.table_awaits_migration`.
 */
export function tableAwaitsMigration(table: unknown): boolean {
  return (
    table instanceof Y.Map && table.get('v') === undefined
    && table.get('columns') instanceof Y.Array && table.get('rows') instanceof Y.Array
  );
}

/** One accepted merged area in index space over the CURRENT order (inclusive). */
export interface MergeArea {
  r0: number; c0: number; r1: number; c1: number;
  anchorRowId: RowId; anchorColId: ColId;
}

/** A raw `merges` entry: the area's bottom-right cell by id. */
type MergeEnd = { get(key: string): unknown };

/**
 * Accepted merged areas, row-major by anchor. An entry is accepted only if anchor and end
 * ids exist, the end is not above/left of the anchor, the area is >= 2 cells, it does not
 * span the header row and body rows at once, and it does not overlap an earlier accepted
 * area. Backend twin: `resolve_merge_entries` (parity fixture `table-v2.json` → `merges`).
 *
 * INVARIANT(persisted): resolve on read, never repair — the model is not touched.
 * Why: a concurrent removal or merge can leave an entry that no longer describes an area;
 * repairing it on one replica would fork the CRDT, and two resolvers that disagree show the
 * agent a different table than the user sees.
 */
export function resolveMergeEntries(
  rowIds: readonly string[], colIds: readonly string[], merges: Iterable<[string, unknown]>,
): MergeArea[] {
  const rowAt = new Map(rowIds.map((id, i) => [id, i]));
  const colAt = new Map(colIds.map((id, i) => [id, i]));
  const candidates: [number, number, number, number][] = [];
  for (const [key, end] of merges) {
    const [ar, ac] = key.split(':');
    const r0 = rowAt.get(ar);
    const c0 = colAt.get(ac);
    if (r0 === undefined || c0 === undefined || !end || typeof (end as MergeEnd).get !== 'function') continue;
    const r1 = rowAt.get(String((end as MergeEnd).get('r')));
    const c1 = colAt.get(String((end as MergeEnd).get('c')));
    if (r1 === undefined || c1 === undefined) continue;
    candidates.push([r0, c0, r1, c1]);
  }
  candidates.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2] || a[3] - b[3]);
  const accepted: MergeArea[] = [];
  for (const [r0, c0, r1, c1] of candidates) {
    if (r1 < r0 || c1 < c0 || (r1 - r0 + 1) * (c1 - c0 + 1) < 2) continue;
    if (r0 === 0 && r1 > 0) continue;
    if (accepted.some((a) => r0 <= a.r1 && a.r0 <= r1 && c0 <= a.c1 && a.c0 <= c1)) continue;
    accepted.push({ r0, c0, r1, c1, anchorRowId: rowIds[r0] as RowId, anchorColId: colIds[c0] as ColId });
  }
  return accepted;
}

function mergesMap(table: TableMap): Y.Map<Y.Map<string>> | null {
  const merges = table.get('merges');
  return merges instanceof Y.Map ? merges as Y.Map<Y.Map<string>> : null;
}

/** Accepted merged areas of a table over its live order; `[]` for a missing/non-v2 table
 *  or one without merges. */
export function resolveMerges(doc: Y.Doc, id: string): MergeArea[] {
  const table = tableById(doc, id);
  const merges = table ? mergesMap(table) : null;
  if (!table || !merges || merges.size === 0) return [];
  const order = tableOrder(doc, id);
  return order ? resolveMergeEntries(order.rowIds, order.colIds, merges.entries()) : [];
}

/** Every covered cell (inside an accepted area, not its anchor) → that area. */
export function coveredCells(
  areas: readonly MergeArea[], order: { rowIds: readonly RowId[]; colIds: readonly ColId[] },
): Map<string, MergeArea> {
  const out = new Map<string, MergeArea>();
  for (const a of areas) {
    for (let r = a.r0; r <= a.r1; r++) {
      for (let c = a.c0; c <= a.c1; c++) {
        if (r !== a.r0 || c !== a.c0) out.set(cellKey(order.rowIds[r], order.colIds[c]), a);
      }
    }
  }
  return out;
}

/** Internal to the table-* modules. */
export interface TableV2Parts {
  columns: Y.Map<AxisEntry>;
  rows: Y.Map<AxisEntry>;
  cells: Y.Map<CellText>;
}

/** `(columns, rows, cells)` of a v2 table Map, `null` for anything else (a v1 table
 * awaiting migration reads as structurally broken, never half-parsed). Backend twin:
 * `_v2_parts`. */
function v2Parts(table: unknown): TableV2Parts | null {
  if (!(table instanceof Y.Map) || table.get('v') !== TABLE_SCHEMA_VERSION) return null;
  const columns = table.get('columns');
  const rows = table.get('rows');
  const cells = table.get('cells');
  if (!(columns instanceof Y.Map) || !(rows instanceof Y.Map) || !(cells instanceof Y.Map)) return null;
  return { columns, rows, cells };
}

function tableById(doc: Y.Doc, id: string): TableMap | undefined {
  return getTablesMap(doc).get(id);
}

/** `tableById` + `v2Parts` in one step — the standard write-path guard. */
function v2ById(doc: Y.Doc, id: string): TableV2Parts | null {
  const table = tableById(doc, id);
  return table ? v2Parts(table) : null;
}

/** `v2Parts` + the cached display order of one table; `null` for a missing/non-v2 table. */
function tableOrder(doc: Y.Doc, id: string): ({ parts: TableV2Parts } & { rowIds: RowId[]; colIds: ColId[] }) | null {
  const table = tableById(doc, id);
  const parts = table && v2Parts(table);
  return parts ? { parts, ...cachedOrder(table, parts) } : null;
}

/** Row ids of a table in display order; `null` for a missing/non-v2 table. */
export function orderedRowIds(doc: Y.Doc, id: string): RowId[] | null {
  return tableOrder(doc, id)?.rowIds ?? null;
}

/** Column ids of a table in display order; `null` for a missing/non-v2 table. */
export function orderedColIds(doc: Y.Doc, id: string): ColId[] | null {
  return tableOrder(doc, id)?.colIds ?? null;
}

// ─── Reads ────────────────────────────────────────────────────────────────────

/**
 * Read a table into the flat whole-matrix form; `null` for a missing/structurally
 * broken entry (including a v1 table awaiting migration — the widget renders the
 * "awaits migration" error for those, never an empty grid).
 *
 * A missing cell (the concurrent add-row × add-column intersection) reads empty;
 * orphan cells (a row or column removed concurrently) are never read.
 */
export function readTableModel(doc: Y.Doc, id: string): TableModel | null {
  const order = tableOrder(doc, id);
  if (!order) return null;
  const { columns, cells } = order.parts;
  return {
    columns: order.colIds.map((cid) => Number(columns.get(cid)?.get('w') ?? DEFAULT_COLUMN_WIDTH)),
    rows: order.rowIds.map((rid) =>
      order.colIds.map((cid) => cells.get(cellKey(rid, cid))?.toString() ?? '')),
  };
}

/**
 * Cheap structural probe — row/column counts ONLY, no cell-text materialization.
 *
 * WHY: `readTableModel` calls `.toString()` on every cell's Y.Text (O(rows×cols) string
 * allocation); the document's table list (`listDocumentTables`) needs only the counts.
 * (The widget short-circuits a keystroke on the order cache's array identity instead.)
 * Returns null for a missing/structurally broken entry. A v1 table still reports its
 * counts (from the legacy arrays) so the panel keeps it reachable.
 */
export function readTableShape(doc: Y.Doc, id: string): { rows: number; cols: number } | null {
  const table = tableById(doc, id);
  if (!table) return null;
  const parts = v2Parts(table);
  if (parts) return { rows: parts.rows.size, cols: parts.columns.size };
  if (tableAwaitsMigration(table)) {
    const columns = table.get('columns') as Y.Array<unknown>;
    const rows = table.get('rows') as Y.Array<unknown>;
    return { rows: rows.length, cols: columns.length };
  }
  return null;
}

/**
 * Column widths in display order, without materializing cell text (the widget reads
 * them on every axis write — a width drag). `null` for a missing/non-v2 table.
 */
export function readColumnWidths(doc: Y.Doc, id: string): ColumnWidth[] | null {
  const order = tableOrder(doc, id);
  if (!order) return null;
  return order.colIds.map((cid) => ({ id: cid, w: Number(order.parts.columns.get(cid)?.get('w') ?? DEFAULT_COLUMN_WIDTH) }));
}

/**
 * The live per-cell Y.Text (for binding a nested editor or scanning text); `null` when
 * the table, row or column does not exist or the cell was never created. A read-only
 * walker treats null as the empty string; an editor-mount path uses `ensureCell`.
 */
export function getCellText(doc: Y.Doc, id: string, rowId: RowId, colId: ColId): Y.Text | null {
  const parts = v2ById(doc, id);
  if (!parts) return null;
  if (!parts.rows.has(rowId) || !parts.columns.has(colId)) return null;
  return parts.cells.get(cellKey(rowId, colId)) ?? null;
}

/**
 * The live per-cell Y.Text, creating an EMPTY one when the cell does not exist yet (the
 * concurrent add-row × add-column intersection) — "a missing cell reads empty; the first
 * write creates it", and mounting a cell editor IS that first write.
 *
 * WHY `TABLE_SYSTEM_ORIGIN`: the creation carries no user content — an empty cell reads
 * identically to a missing one, so it must NOT enter the undo stack as an invisible
 * Ctrl+Z step. Subsequent typing goes through the yCollab binding's own origin.
 */
export function ensureCell(doc: Y.Doc, id: string, rowId: RowId, colId: ColId): Y.Text | null {
  const parts = v2ById(doc, id);
  if (!parts) return null;
  if (!parts.rows.has(rowId) || !parts.columns.has(colId)) return null;
  const key = cellKey(rowId, colId);
  let text = parts.cells.get(key);
  if (!text) {
    doc.transact(() => {
      parts.cells.set(key, new Y.Text());
    }, TABLE_SYSTEM_ORIGIN);
    text = parts.cells.get(key)!;
  }
  return text;
}

// ─── Writes: structure ────────────────────────────────────────────────────────

/**
 * Write one fresh v2 table under `tableId`: `len(widths)` columns, one row per matrix
 * row, a cell per row × column (EAGER). Caller owns the transaction and its origin —
 * `createTable` (user), `applyTablesJson` (seeding) and tests all build through here.
 * Backend twin: `_build_table`.
 *
 * ARCH: build every child map FULLY, then attach once. Pre-built Y.Map/Y.Text children
 * integrate correctly when inserted in bulk, so a 5000×200 import is O(rows+cols) map
 * sets, never one Yjs operation per push (the v1 rule, kept).
 *
 * Internal to the table-* modules.
 */
export function buildTable(
  tables: Y.Map<TableMap>, tableId: string, widths: number[], matrix: string[][],
  merges: readonly unknown[] = [],
): void {
  const columns = new Y.Map<AxisEntry>();
  const rows = new Y.Map<AxisEntry>();
  const cells = new Y.Map<CellText>();
  const colIds = widths.map(() => uuid() as ColId);
  const rowIds: RowId[] = [];
  generateNKeysBetween(null, null, colIds.length).forEach((pos, i) => {
    const entry = new Y.Map<string | number>() as AxisEntry;
    entry.set('pos', pos);
    entry.set('w', widths[i]);
    columns.set(colIds[i], entry);
  });
  generateNKeysBetween(null, null, matrix.length).forEach((pos, r) => {
    const rid = uuid() as RowId;
    rowIds.push(rid);
    const entry = new Y.Map<string | number>() as AxisEntry;
    entry.set('pos', pos);
    rows.set(rid, entry);
    colIds.forEach((cid, c) => {
      const body = new Y.Text(matrix[r][c] ?? '');
      cells.set(cellKey(rid, cid), body);
    });
  });
  const table = new Y.Map() as TableMap;
  table.set('v', TABLE_SCHEMA_VERSION);
  table.set('columns', columns);
  table.set('rows', rows);
  table.set('cells', cells);
  // WHY always a map: two peers creating it concurrently on their first merge would
  // overwrite each other's entry (a nested map set is last-writer-wins).
  const mergeEntries = new Y.Map<Y.Map<string>>();
  for (const m of merges) {
    if (!mergeInGrid(m, rowIds.length, colIds.length)) continue;
    const [r0, c0, r1, c1] = m;
    mergeEntries.set(cellKey(rowIds[r0], colIds[c0]), mergeEnd(rowIds[r1], colIds[c1]));
  }
  table.set('merges', mergeEntries);
  tables.set(tableId, table);
}

/** A checkpoint `[r0, c0, r1, c1]` whose four indices address the grid (backend twin
 *  `_merge_in_grid`). */
function mergeInGrid(m: unknown, nRows: number, nCols: number): m is [number, number, number, number] {
  if (!Array.isArray(m) || m.length !== 4 || !m.every((i) => Number.isInteger(i))) return false;
  const [r0, c0, r1, c1] = m as number[];
  return r0 >= 0 && r0 < nRows && r1 >= 0 && r1 < nRows && c0 >= 0 && c0 < nCols && c1 >= 0 && c1 < nCols;
}

function mergeEnd(rowId: RowId, colId: ColId): Y.Map<string> {
  const end = new Y.Map<string>();
  end.set('r', rowId);
  end.set('c', colId);
  return end;
}

/**
 * Build a `tables` entry from a cell-body matrix; returns the fresh uuid. `total` = birth
 * width in px, `layout` = the width rule (the shared birth rule unless a client fits the
 * table to its own document width).
 */
export function createTable(
  doc: Y.Doc, matrix: string[][], total = BIRTH_TABLE_WIDTH,
  layout: (matrix: string[][], total: number) => number[] = birthColumnWidths,
): string {
  const id = uuid();
  doc.transact(() => {
    buildTable(getTablesMap(doc), id, layout(matrix, total), matrix);
  }, TABLE_LOCAL_ORIGIN);
  return id;
}

/** Deep-copy a table under a fresh id (copy-paste of an anchor — never alias one model). */
export function cloneTable(doc: Y.Doc, srcId: string): string {
  const model = readTableModel(doc, srcId);
  if (!model) throw new Error(`cloneTable: source table ${srcId} not found`);
  const merges = resolveMerges(doc, srcId).map((a) => [a.r0, a.c0, a.r1, a.c1]);
  const newId = uuid();
  doc.transact(() => {
    buildTable(getTablesMap(doc), newId, model.columns, model.rows, merges);
  }, TABLE_LOCAL_ORIGIN);
  return newId;
}

export function deleteTable(doc: Y.Doc, id: string): void {
  doc.transact(() => {
    getTablesMap(doc).delete(id);
  }, TABLE_LOCAL_ORIGIN);
}

/**
 * Add ONE row. `afterId` = the row that ends up right ABOVE the new one, `beforeId` =
 * right BELOW (both `null` = append at the BOTTOM — the Tab/agent append path). Cells
 * are EAGER: a cell per live column, so a replica never sees a row without its cells.
 * Returns the new `RowId`, `null` when the table does not exist.
 */
export function addRow(doc: Y.Doc, id: string, beforeId: RowId | null = null, afterId: RowId | null = null): RowId | null {
  const order = tableOrder(doc, id);
  if (!order) return null;
  const { parts } = order;
  const rid = uuid() as RowId;
  localStep(doc, () => {
    const pos = beforeId === null && afterId === null
      ? appendPos(parts.rows)
      : posBetween(parts.rows, beforeId, afterId);
    const entry = new Y.Map<string | number>() as AxisEntry;
    entry.set('pos', pos);
    parts.rows.set(rid, entry);
    for (const cid of order.colIds) {
      parts.cells.set(cellKey(rid, cid), new Y.Text());
    }
  });
  return rid;
}

/**
 * Insert ONE column between `beforeId` (the column that ends up right BEFORE it,
 * `null` = first) and `afterId` (right AFTER it, `null` = last); both `null` = append
 * at the END (the default-add path, mirrors `addRow`). A cell per live row
 * (EAGER), all in ONE transaction. Returns the new `ColId`, `null` when the table does
 * not exist. `width`: the px width the caller inherits (the cell menu passes the clicked
 * column's), floored at `MIN_COLUMN_WIDTH` — see INVARIANT on `fixedWidth`.
 */
export function addColumn(
  doc: Y.Doc, id: string, width: number, beforeId: ColId | null = null, afterId: ColId | null = null,
): ColId | null {
  const parts = v2ById(doc, id);
  if (!parts) return null;
  const cid = uuid() as ColId;
  localStep(doc, () => {
    const entry = new Y.Map<string | number>() as AxisEntry;
    entry.set('pos', beforeId === null && afterId === null
      ? appendPos(parts.columns)
      : posBetween(parts.columns, beforeId, afterId));
    entry.set('w', fixedWidth(width));
    parts.columns.set(cid, entry);
    for (const rid of parts.rows.keys() as Iterable<RowId>) {
      parts.cells.set(cellKey(rid, cid), new Y.Text());
    }
  });
  return cid;
}

/**
 * Remove one row AND its cells (`"<rowId>:"` prefix) in ONE transaction — a replica
 * never sees a row without its cells, and no orphan cell is left behind by a local op
 * (orphans exist only via the concurrent remove × add races, and every reader ignores
 * them — see `readTableModel`). No-op when the table or row does not exist.
 */
export function removeRow(doc: Y.Doc, id: string, rowId: RowId): void {
  const parts = v2ById(doc, id);
  if (!parts || !parts.rows.has(rowId)) return;
  localStep(doc, () => {
    shrinkMerges(doc, id, 'row', rowId);
    parts.rows.delete(rowId);
    const prefix = `${rowId}:`;
    for (const key of Array.from(parts.cells.keys())) {
      if (key.startsWith(prefix)) parts.cells.delete(key);
    }
  });
}

/**
 * Empty the text of every named cell in ONE local step — one undo item for the whole
 * range. A missing cell is skipped, never created; an already-empty one is not touched.
 */
export function clearCells(doc: Y.Doc, id: string, cells: readonly { rowId: RowId; colId: ColId }[]): void {
  const order = tableOrder(doc, id);
  if (!order) return;
  const { parts } = order;
  const covered = coveredCells(resolveMerges(doc, id), order);
  const texts = cells
    .filter((c) => !covered.has(cellKey(c.rowId, c.colId)))
    .map((c) => parts.cells.get(cellKey(c.rowId, c.colId)))
    .filter((text): text is Y.Text => !!text && text.length > 0);
  if (texts.length === 0) return;
  localStep(doc, () => {
    for (const text of texts) text.delete(0, text.length);
  });
}

/** Remove one column and its cells (`:"<colId>"` suffix) — column twin of `removeRow`. */
export function removeColumn(doc: Y.Doc, id: string, colId: ColId): void {
  const parts = v2ById(doc, id);
  if (!parts || !parts.columns.has(colId)) return;
  localStep(doc, () => {
    shrinkMerges(doc, id, 'col', colId);
    parts.columns.delete(colId);
    const suffix = `:${colId}`;
    for (const key of Array.from(parts.cells.keys())) {
      if (key.endsWith(suffix)) parts.cells.delete(key);
    }
  });
}

/**
 * Move one row/column to sit right after `beforeId` / right before `afterId` — ONE
 * `pos` write, no reorder of any other item (the whole point of fractional keys). A
 * bound naming the moved item itself, or an anchor the race already removed, degrades
 * to that side's END (see `posBetween`). No-op when the table or item does not exist.
 */
export function moveRow(doc: Y.Doc, id: string, rowId: RowId, beforeId: RowId | null = null, afterId: RowId | null = null): void {
  const parts = v2ById(doc, id);
  if (!parts) return;
  const entry = parts.rows.get(rowId);
  if (!entry) return;
  const lo = beforeId === rowId ? null : beforeId;
  const hi = afterId === rowId ? null : afterId;
  localStep(doc, () => {
    entry.set('pos', posBetween(parts.rows, lo, hi));
  });
}

/** Column twin of `moveRow`. */
export function moveColumn(doc: Y.Doc, id: string, colId: ColId, beforeId: ColId | null = null, afterId: ColId | null = null): void {
  const parts = v2ById(doc, id);
  if (!parts) return;
  const entry = parts.columns.get(colId);
  if (!entry) return;
  const lo = beforeId === colId ? null : beforeId;
  const hi = afterId === colId ? null : afterId;
  localStep(doc, () => {
    entry.set('pos', posBetween(parts.columns, lo, hi));
  });
}

// ─── Writes: merged cells ─────────────────────────────────────────────────────

/**
 * Merge the rectangle from the anchor cell to the end cell into one cell, as ONE undo step:
 * every cell entering the area except the anchor is emptied, and any merge entry whose area
 * it absorbs is dropped. No-op when an id is gone or the rectangle is a single cell.
 */
export function setMerge(
  doc: Y.Doc, id: string, anchorRowId: RowId, anchorColId: ColId, endRowId: RowId, endColId: ColId,
): void {
  const table = tableById(doc, id);
  const order = tableOrder(doc, id);
  if (!table || !order) return;
  const [r0, r1] = [order.rowIds.indexOf(anchorRowId), order.rowIds.indexOf(endRowId)];
  const [c0, c1] = [order.colIds.indexOf(anchorColId), order.colIds.indexOf(endColId)];
  if (r0 < 0 || c0 < 0 || r1 < r0 || c1 < c0 || (r0 === r1 && c0 === c1)) return;
  const absorbed = resolveMerges(doc, id).filter((a) => a.r0 <= r1 && r0 <= a.r1 && a.c0 <= c1 && c0 <= a.c1);
  localStep(doc, () => {
    let merges = mergesMap(table);
    if (!merges) {
      merges = new Y.Map<Y.Map<string>>();
      table.set('merges', merges);
    }
    for (const a of absorbed) merges.delete(cellKey(a.anchorRowId, a.anchorColId));
    for (let r = r0; r <= r1; r++) {
      for (let c = c0; c <= c1; c++) {
        if (r === r0 && c === c0) continue;
        const text = order.parts.cells.get(cellKey(order.rowIds[r], order.colIds[c]));
        if (text && text.length > 0) text.delete(0, text.length);
      }
    }
    merges.set(cellKey(anchorRowId, anchorColId), mergeEnd(endRowId, endColId));
  });
}

/** Write checkpoint-shaped areas (`[r0, c0, r1, c1]`, positional over the current order)
 *  onto a freshly created table; entries outside the grid are skipped, the resolver
 *  decides the rest on read. */
export function writeMergeAreas(doc: Y.Doc, id: string, areas: readonly unknown[]): void {
  const table = tableById(doc, id);
  const order = tableOrder(doc, id);
  const merges = table ? mergesMap(table) : null;
  if (!order || !merges) return;
  doc.transact(() => {
    for (const m of areas) {
      if (!mergeInGrid(m, order.rowIds.length, order.colIds.length)) continue;
      const [r0, c0, r1, c1] = m;
      merges.set(cellKey(order.rowIds[r0], order.colIds[c0]), mergeEnd(order.rowIds[r1], order.colIds[c1]));
    }
  }, TABLE_LOCAL_ORIGIN);
}

/** Dissolve the area anchored at the cell; its text stays in the anchor. */
export function unmerge(doc: Y.Doc, id: string, anchorRowId: RowId, anchorColId: ColId): void {
  const table = tableById(doc, id);
  const merges = table ? mergesMap(table) : null;
  const key = cellKey(anchorRowId, anchorColId);
  if (!merges?.has(key)) return;
  localStep(doc, () => { merges.delete(key); });
}

/**
 * Keep every accepted area valid while one row/column of it is removed (call inside the
 * removal's transaction, BEFORE the removal): removing the end line moves the end to the
 * previous line inside the area; removing the anchor's line moves the anchor to the next
 * line inside the area, carrying the anchor text over; an area left with one line along
 * that axis and one cell in total, or with no line at all, is dropped.
 */
function shrinkMerges(doc: Y.Doc, id: string, axis: 'row' | 'col', lineId: string): void {
  const table = tableById(doc, id);
  const order = tableOrder(doc, id);
  const merges = table ? mergesMap(table) : null;
  if (!order || !merges) return;
  const ids: readonly string[] = axis === 'row' ? order.rowIds : order.colIds;
  const i = ids.indexOf(lineId);
  for (const a of resolveMergeEntries(order.rowIds, order.colIds, merges.entries())) {
    const [lo, hi] = axis === 'row' ? [a.r0, a.r1] : [a.c0, a.c1];
    if (i < lo || i > hi) continue;
    const key = cellKey(a.anchorRowId, a.anchorColId);
    const end = merges.get(key)!;
    const cells = (axis === 'row' ? a.c1 - a.c0 + 1 : a.r1 - a.r0 + 1) * (hi - lo);
    if (cells < 2) { merges.delete(key); continue; }
    if (i === hi) { end.set(axis === 'row' ? 'r' : 'c', ids[hi - 1]); continue; }
    if (i !== lo) continue;
    const next = axis === 'row'
      ? { rowId: ids[lo + 1] as RowId, colId: a.anchorColId }
      : { rowId: a.anchorRowId, colId: ids[lo + 1] as ColId };
    const from = order.parts.cells.get(key)?.toString() ?? '';
    const nextKey = cellKey(next.rowId, next.colId);
    let to = order.parts.cells.get(nextKey);
    if (!to) {
      to = new Y.Text();
      order.parts.cells.set(nextKey, to);
    }
    if (to.length > 0) to.delete(0, to.length);
    if (from) to.insert(0, from);
    const moved = mergeEnd(end.get('r') as RowId, end.get('c') as ColId);
    merges.delete(key);
    merges.set(nextKey, moved);
  }
}

// ─── Writes: widths ───────────────────────────────────────────────────────────

// INVARIANT(persisted): every column always has a fixed px width (>= MIN_COLUMN_WIDTH);
// there are no auto columns.
// Why: table widths must be predictable and never reflow by themselves.
const fixedWidth = (w: number): number => Math.max(Math.round(w), MIN_COLUMN_WIDTH);

export function setColumnWidth(doc: Y.Doc, id: string, colId: ColId, width: number): void {
  const parts = v2ById(doc, id);
  const colMap = parts?.columns.get(colId);
  if (!colMap) return;
  localStep(doc, () => {
    colMap.set('w', fixedWidth(width));
  });
}

/** Write a set of column widths in one transaction; only changed values. Ids the table
 * no longer has (a peer removed the column mid-drag) are ignored. */
export function setColumnWidths(doc: Y.Doc, id: string, widths: ColumnWidth[]): void {
  const parts = v2ById(doc, id);
  if (!parts) return;
  localStep(doc, () => {
    for (const { id: colId, w } of widths) {
      const colMap = parts.columns.get(colId);
      if (colMap && colMap.get('w') !== fixedWidth(w)) colMap.set('w', fixedWidth(w));
    }
  });
}

// ─── Label / anchor / whole-table paths ──────────────────────────────────────

/**
 * Store a fallback label ON the model (a `label` field in the table's Y.Map).
 *
 * WHY: the display label of a LINKED table lives in its `![label](table:id)` anchor. An
 * UNLINKED table (anchor removed/corrupted) has no anchor to hold a name, so a rename would
 * have nowhere to persist. This model-level label is the fallback: it syncs as part of the
 * `tables` CRDT subtree (so a peer/reconnect keeps it) and seeds the anchor when the table is
 * re-inserted. It is NOT part of the serialized shape (see `serializeTables`) — a cosmetic
 * orphan label is intentionally not checkpoint-persisted, keeping the frontend↔backend shape
 * INVARIANT intact. Stored normalized so it can seed a valid anchor.
 */
export function setModelLabel(doc: Y.Doc, id: string, label: string): void {
  const table = tableById(doc, id);
  if (table) localStep(doc, () => {
    table.set('label', label);
  });
}

/** Read the model-level fallback label (see `setModelLabel`); '' when unset. */
export function getModelLabel(doc: Y.Doc, id: string): string {
  const table = tableById(doc, id);
  const v = table ? table.get('label') : undefined;
  return typeof v === 'string' ? v : '';
}
