/**
 * Table-block model — read/write helpers for the `tables` Yjs subtree.
 *
 * SYSTEM: table-block — frontend model entry point. The editable table object stores its
 * data OUTSIDE the plain-text buffer, as a sibling root in the same entity Y.Doc:
 *
 *     ydoc.getMap('tables') : Y.Map<tableId, Y.Map{
 *       columns: Y.Array<Y.Map{ w: number }>,            // px width per column
 *       rows:    Y.Array<Y.Array<Y.Map{ t: Y.Text }>>,   // cell = Y.Map, body = Y.Text
 *     }>
 *
 * The document `content` text holds only a transclusion anchor `![label](table:id)`.
 *
 * ARCH: cell body is a per-cell Y.Text so concurrent edits to different cells (and width
 * drags) never conflict and never produce an invalid intermediate state — the reason the
 * model is a CRDT subtree, not a JSON string in the text buffer.
 *
 * INVARIANT: these helpers are the ONLY sanctioned write path; the widget is a pure
 * reflection of this model (Thin Client). Why: keeps collab merge correct and the widget
 * stateless. Every mutation runs inside a single `doc.transact` so one widget action = one
 * atomic Yjs update.
 *
 * INVARIANT(data-loss): an orphan MODEL (no anchor references its id) is TOLERATED — it is
 * retained, never auto-pruned. Only an orphan ANCHOR (anchor present, no model) renders an
 * inline error. Why: "no matching anchor" cannot distinguish a deliberate anchor removal
 * from a corrupted `table:id`, so auto-cleanup silently lost whole tables on a single
 * mangled char. Deletion is EXPLICIT-UI-ONLY (`deleteTableWithBackup`); a retained orphan is
 * accepted dead weight (no GC). `deleteOrphanModels` survives only for the composed
 * `reconcileTables` helper (tests / a possible future manual "clean unused"), NOT as an auto
 * caller — see `table-reconcile.ts`.
 */
import * as Y from 'yjs';
import { uuid } from '../../../utils/uuid';

/** Default column width in px for new columns / freshly created tables. */
export const DEFAULT_COLUMN_WIDTH = 160;

export interface TableModel {
  columns: number[];
  rows: string[][];
}

type ColMap = Y.Map<number>; // { w: number }
type CellMap = Y.Map<Y.Text>; // { t: Y.Text }
type TableMap = Y.Map<Y.Array<ColMap> | Y.Array<Y.Array<CellMap>>>;

export function getTablesMap(doc: Y.Doc): Y.Map<TableMap> {
  return doc.getMap('tables') as Y.Map<TableMap>;
}

function getColumns(table: TableMap): Y.Array<ColMap> {
  return table.get('columns') as Y.Array<ColMap>;
}

function getRows(table: TableMap): Y.Array<Y.Array<CellMap>> {
  return table.get('rows') as Y.Array<Y.Array<CellMap>>;
}

function makeColumn(width: number): ColMap {
  const col = new Y.Map() as ColMap;
  col.set('w', width);
  return col;
}

function makeCell(text: string): CellMap {
  const cell = new Y.Map() as CellMap;
  const body = new Y.Text();
  if (text) body.insert(0, text);
  cell.set('t', body);
  return cell;
}

/** Build a `tables` entry from a cell-body matrix; returns the fresh uuid. */
export function createTable(doc: Y.Doc, matrix: string[][]): string {
  const id = uuid();
  const width = matrix.reduce((m, r) => Math.max(m, r.length), 1);
  doc.transact(() => {
    const table = new Y.Map() as TableMap;
    // ARCH: batch every child array into ONE Yjs `push` instead of one push per item.
    // Why: each `push` is a separate Yjs operation (observer fire + op in the update). A
    // 5000×200 import pushed cell-by-cell emitted ~1M ops; batched it is O(rows+2) pushes.
    // Pre-built Y.Map/Y.Array/Y.Text children integrate correctly when inserted in bulk.
    const columns = new Y.Array<ColMap>();
    columns.push(Array.from({ length: width }, () => makeColumn(DEFAULT_COLUMN_WIDTH)));
    const rows = new Y.Array<Y.Array<CellMap>>();
    rows.push(matrix.map((row) => {
      const yrow = new Y.Array<CellMap>();
      yrow.push(Array.from({ length: width }, (_, c) => makeCell(row[c] ?? '')));
      return yrow;
    }));
    table.set('columns', columns);
    table.set('rows', rows);
    getTablesMap(doc).set(id, table);
  });
  return id;
}

/** Read a table into the flat model form; null for a missing/structurally broken entry. */
export function readTableModel(doc: Y.Doc, id: string): TableModel | null {
  const table = getTablesMap(doc).get(id);
  if (!table) return null;
  const columns = getColumns(table);
  const rows = getRows(table);
  if (!(columns instanceof Y.Array) || !(rows instanceof Y.Array)) return null;
  return {
    columns: columns.map((c) => c.get('w') as number),
    rows: rows.map((row) => row.map((cell) => (cell.get('t') as Y.Text).toString())),
  };
}

/**
 * Cheap structural probe — row/column counts ONLY, no cell-text materialization.
 *
 * ARCH: `readTableModel` calls `.toString()` on every cell's Y.Text (O(rows×cols) string
 * allocation). The block widget's `observeDeep` callback fires on every keystroke in any cell,
 * so calling `readTableModel` per fire would allocate the whole table per keystroke. This
 * helper reads only the Yjs array lengths, letting the widget short-circuit a no-op re-render
 * (same row/column counts → content-only change, patched in place by yCollab) without ever
 * touching cell text. Returns null for a missing/structurally broken entry.
 */
export function readTableShape(doc: Y.Doc, id: string): { rows: number; cols: number } | null {
  const table = getTablesMap(doc).get(id);
  if (!table) return null;
  const columns = getColumns(table);
  const rows = getRows(table);
  if (!(columns instanceof Y.Array) || !(rows instanceof Y.Array)) return null;
  return { rows: rows.length, cols: columns.length };
}

/** Read only the column widths (px), without materializing cell text. See `readTableShape`. */
export function readColumnWidths(doc: Y.Doc, id: string): number[] | null {
  const table = getTablesMap(doc).get(id);
  if (!table) return null;
  const columns = getColumns(table);
  if (!(columns instanceof Y.Array)) return null;
  return columns.map((c) => c.get('w') as number);
}

/** The live per-cell Y.Text (for binding a nested editor); null if out of range. */
export function getCellText(doc: Y.Doc, id: string, row: number, col: number): Y.Text | null {
  const table = getTablesMap(doc).get(id);
  if (!table) return null;
  const cell = getRows(table).get(row)?.get(col);
  if (!cell) return null;
  const body = cell.get('t');
  return body instanceof Y.Text ? body : null;
}

/** Deep-copy a table under a fresh id (copy-paste of an anchor — never alias one model). */
export function cloneTable(doc: Y.Doc, srcId: string): string {
  const model = readTableModel(doc, srcId);
  if (!model) throw new Error(`cloneTable: source table ${srcId} not found`);
  const newId = createTable(doc, model.rows);
  doc.transact(() => {
    model.columns.forEach((w, i) => setColumnWidth(doc, newId, i, w));
  });
  return newId;
}

export function deleteTable(doc: Y.Doc, id: string): void {
  getTablesMap(doc).delete(id);
}

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
function setModelLabel(doc: Y.Doc, id: string, label: string): void {
  const table = getTablesMap(doc).get(id);
  if (table) (table as unknown as Y.Map<unknown>).set('label', label);
}

/** Read the model-level fallback label (see `setModelLabel`); '' when unset. */
export function getModelLabel(doc: Y.Doc, id: string): string {
  const table = getTablesMap(doc).get(id);
  const v = table ? (table as unknown as Y.Map<unknown>).get('label') : undefined;
  return typeof v === 'string' ? v : '';
}

/**
 * Create a table model from a cell matrix AND store its label — with NO anchor spliced into
 * the document `content`. Returns the fresh table id.
 *
 * WHY: the CSV/TSV panel-import path (see `csv-table.ts`) absorbs a file fully into a table
 * model; the table then surfaces as a panel-resident "virtual reference" badge (reactively
 * via `useDocumentTables`) without occupying a line in the body. The label is the model-level
 * fallback (see `setModelLabel`) — it seeds the anchor if the user later inserts the table.
 *
 * INVARIANT(atomic-write): model build + label set run in ONE `doc.transact`.
 * Why: two separate transactions could be observed mid-way by a peer or by the reactive
 * `useDocumentTables` derive — a badge with an empty label that then snaps to the real name.
 * One atomic update makes an import a single consistent table-with-label Yjs state.
 */
export function createUnlinkedTable(doc: Y.Doc, matrix: string[][], label: string): string {
  let id = '';
  doc.transact(() => {
    id = createTable(doc, matrix);
    setModelLabel(doc, id, normalizeTableLabel(label));
  });
  return id;
}

export function setCellText(doc: Y.Doc, id: string, row: number, col: number, text: string): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const cell = getRows(table).get(row)?.get(col);
  if (!cell) return;
  const body = cell.get('t') as Y.Text;
  doc.transact(() => {
    if (body.length) body.delete(0, body.length);
    if (text) body.insert(0, text);
  });
}

export function setColumnWidth(doc: Y.Doc, id: string, col: number, width: number): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const colMap = getColumns(table).get(col);
  if (colMap) colMap.set('w', width);
}

export function addRow(doc: Y.Doc, id: string, at?: number): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const rows = getRows(table);
  const width = getColumns(table).length;
  doc.transact(() => {
    const yrow = new Y.Array<CellMap>();
    for (let c = 0; c < width; c++) yrow.push([makeCell('')]);
    rows.insert(at ?? rows.length, [yrow]);
  });
}

export function addColumn(doc: Y.Doc, id: string, at?: number): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const columns = getColumns(table);
  const rows = getRows(table);
  const index = at ?? columns.length;
  doc.transact(() => {
    columns.insert(index, [makeColumn(DEFAULT_COLUMN_WIDTH)]);
    rows.forEach((row) => row.insert(index, [makeCell('')]));
  });
}

export function removeRow(doc: Y.Doc, id: string, index: number): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const rows = getRows(table);
  if (index < 0 || index >= rows.length) return;
  doc.transact(() => rows.delete(index, 1));
}

export function removeColumn(doc: Y.Doc, id: string, index: number): void {
  const table = getTablesMap(doc).get(id);
  if (!table) return;
  const columns = getColumns(table);
  const rows = getRows(table);
  if (index < 0 || index >= columns.length) return;
  doc.transact(() => {
    columns.delete(index, 1);
    rows.forEach((row) => {
      if (index < row.length) row.delete(index, 1);
    });
  });
}

/** Build a table transclusion anchor for the document text. */
export function tableAnchor(label: string, id: string): string {
  return `![${label}](table:${id})`;
}

/** Matches every `![label](table:id)` anchor; group 1 = label, group 2 = id. */
export const TABLE_ANCHOR_RE = /!\[([^\]]*)\]\(table:([^)]+)\)/g;

/**
  * Reconcile the `tables` model against the document content (anchor ↔ model lifecycle):
  *
  *  - A DUPLICATED anchor id (copy-paste) is cloned under a fresh id and the content is
  *    rewritten so each anchor owns a distinct model (no aliasing).
  *  - A model whose anchor is gone (deleted line) is removed (orphan cleanup).
  *
  * Returns the (possibly rewritten) content + a `changed` flag. The caller is responsible
  * for writing `content` back into the text buffer when `changed` (the model mutations are
  * already applied to the Y.Doc here).
  *
  * INVARIANT: an orphan ANCHOR (anchor present, no model) is NOT created here — it renders
  * an inline error in the widget. Only orphan MODELS are cleaned up. Why: content is the
  * user's authored text; we never silently delete an anchor they typed.
  *
  * ARCH: the two passes are split (`cloneDuplicateAnchors` + `deleteOrphanModels`) so the
  * live trigger can run them on different timers — clone (latency-sensitive, paste) on a short
  * debounce; orphan cleanup on a longer idle timer. Why: `insertTable`/paste emit the model
  * and its anchor as SEPARATE Yjs updates; a peer that has the model but not yet the anchor,
  * and whose own typing fires the short timer in between, must NOT have that just-synced model
  * deleted as an "orphan" before its anchor arrives. Deferring cleanup gives the anchor a wide
  * window to land. This composed function runs both immediately (used by tests / explicit save).
  */
export function cloneDuplicateAnchors(doc: Y.Doc, content: string): { content: string; changed: boolean } {
  const tables = getTablesMap(doc);
  const seen = new Set<string>();
  const referenced = new Set<string>();
  let changed = false;

  const rewritten = content.replace(TABLE_ANCHOR_RE, (full, label: string, id: string) => {
    if (!seen.has(id)) {
      seen.add(id);
      referenced.add(id);
      return full;
    }
    // Duplicate id → clone the source model under a fresh id, rewrite this anchor.
    if (!tables.has(id)) {
      // Orphan duplicate (no model to clone) — leave as-is; widget shows the error.
      return full;
    }
    const newId = cloneTable(doc, id);
    changed = true;
    seen.add(newId);
    referenced.add(newId);
    return tableAnchor(label, newId);
  });

  return { content: rewritten, changed };
}

/** Drop any `tables` model that no surviving anchor in `content` references. */
export function deleteOrphanModels(doc: Y.Doc, content: string): void {
  const tables = getTablesMap(doc);
  const referenced = new Set<string>();
  for (const m of content.matchAll(TABLE_ANCHOR_RE)) referenced.add(m[2]);
  for (const key of [...tables.keys()]) {
    if (!referenced.has(key)) deleteTable(doc, key);
  }
}

export function reconcileTables(doc: Y.Doc, content: string): { content: string; changed: boolean } {
  const { content: rewritten, changed } = cloneDuplicateAnchors(doc, content);
  deleteOrphanModels(doc, rewritten);
  return { content: rewritten, changed };
}

/**
 * Serialize the whole `tables` subtree to compact JSON for checkpoint capture.
 *
 * INVARIANT (shared shape, frontend ↔ backend): the output MUST be byte-identical in
 * SHAPE to the backend `capture_tables_json`: `{ "<id>": { columns: number[], rows: string[][] } }`.
 * Why: a checkpoint is restored by whichever replica/producer captured it; silent shape
 * drift (e.g. dropping column widths) rebuilds a default-width table. `table-block-model.test.ts`
 * shares one fixture with the backend `test_table_capture.py` to guard this.
 *
 * `'{}'` when there are no tables. Capture is lossless (cell text verbatim, no GFM escaping).
 */
export function serializeTables(doc: Y.Doc): string {
  const tables = getTablesMap(doc);
  const out: Record<string, { columns: number[]; rows: string[][] }> = {};
  tables.forEach((table, id) => {
    const model = readTableModel(doc, id);
    if (!model) return;
    out[id] = { columns: model.columns, rows: model.rows };
  });
  return JSON.stringify(out);
}

/**
 * Rebuild the `tables` subtree from `serializeTables` output (checkpoint restore).
 *
 * `null` = legacy restore (no tables captured): leave the tables map untouched.
 * `'{}'` / empty = clear to no tables. Otherwise clear + rebuild every table under fresh
 * ids in ONE `doc.transact()` so the rebuild is a single atomic Yjs update — the broadcast
 * frame then carries the whole table rebuild and other replicas converge without an
 * intermediate orphan-anchor state. Mirrors the backend `apply_tables_json`.
 */
export function applyTablesJson(doc: Y.Doc, tablesJson: string | null): void {
  if (tablesJson === null) return;
  const data: Record<string, { columns?: number[]; rows?: string[][] }> = JSON.parse(tablesJson || '{}');
  const tables = getTablesMap(doc);

  doc.transact(() => {
    tables.forEach((_, id) => tables.delete(id));
    Object.entries(data).forEach(([id, model]) => {
      const columns = model.columns ?? [];
      const rows = model.rows ?? [];
      let width = columns.length;
      if (width === 0 && rows.length) width = Math.max(...rows.map((r) => r.length));
      const table = new Y.Map() as TableMap;
      tables.set(id, table);

      const colsArr = new Y.Array<ColMap>();
      table.set('columns', colsArr);
      for (let i = 0; i < width; i++) {
        const col = new Y.Map() as ColMap;
        colsArr.push([col]);
        col.set('w', columns[i] ?? DEFAULT_COLUMN_WIDTH);
      }

      const rowsArr = new Y.Array<Y.Array<CellMap>>();
      table.set('rows', rowsArr);
      for (const row of rows) {
        const yrow = new Y.Array<CellMap>();
        rowsArr.push([yrow]);
        for (let c = 0; c < width; c++) {
          const cell = new Y.Map() as CellMap;
          yrow.push([cell]);
          cell.set('t', new Y.Text(row[c] ?? ''));
        }
      }
    });
  });
}

/**
 * Serialize a table model to GFM pipe-table markdown (the byte-inverse of
 * `gfm-table-import.ts`'s `parseRow`/`unescapeCell`).
 *
 * model→GFM export (clipboard copy path; see SYSTEM: table-block). The editable table lives as
 * a Yjs subtree; this is the read-only serializer that turns it back into portable pipe
 * markdown so a copied table becomes real `| a | b |` text in `text/plain` and a `<table>`
 * in `text/html` (via `cleanMarkdownToHtml`). Never throws.
 *
 * INVARIANT: cell escaping is the exact inverse of the importer — `\n`→`<br>`, `|`→`\|`,
 * exactly one padding space per cell side, `rows[0]` = header, separator = `| --- | … |`.
 * Why: a copy must round-trip back into a native table object on paste (see
 * `gfm-table-import.ts:unescapeCell`/`parseRow`). Read-only on the Y.Doc.
 *
 * Known limitation (inherent to GFM, not a bug): a header ROW whose EVERY cell body matches
 * the separator grammar `/^:?-+:?$/` (e.g. `| --- | :--: |`) cannot be represented — GFM has
 * no way to mark such a row as data rather than separator, so the importer misclassifies it
 * (gfm-table-import.ts:isSeparatorRow) and the table is not recognized. A single separator-
 * grammar cell alongside a normal cell is fine (the row only reads as a separator when all
 * its cells are dash-like). Rare in practice; body cells are unaffected.
 */
export function modelToGfm(model: TableModel): string {
  const rows = model.rows;
  if (rows.length === 0) return '';
  const width = Math.max(model.columns.length, ...rows.map((r) => r.length), 1);
  // Inverse of unescapeCell: pipe first (so a cell's own `|` is escaped before padding),
  // then newline→<br>. Order is load-bearing for invertibility across mixed cell content.
  const esc = (cell: string) => cell.replace(/\|/g, '\\|').replace(/\n/g, '<br>');
  const rowToLine = (cells: string[]) => {
    const padded = cells.slice(0, width);
    while (padded.length < width) padded.push('');
    return `|${padded.map((c) => ` ${esc(c)} `).join('|')}|`;
  };
  const header = rowToLine(rows[0]);
  const sep = `|${Array.from({ length: width }, () => ' --- ').join('|')}|`;
  const body = rows.slice(1).map(rowToLine);
  return [header, sep, ...body].join('\n');
}

/**
 * Replace every `![label](table:id)` anchor in `markdown` with the GFM serialization of its
 * model. A model that is missing (orphan anchor / mount race) is left untouched — the copy
 * handler degrades gracefully and never throws. Read-only on the Y.Doc.
 */
export function expandTableAnchorsToGfm(doc: Y.Doc, markdown: string): string {
  return markdown.replace(TABLE_ANCHOR_RE, (full, _label: string, id: string) => {
    const model = readTableModel(doc, id);
    if (!model) return full;
    return modelToGfm(model);
  });
}

/**
 * Delete a WHOLE table (anchor + model) — the References-panel "table badge" delete path.
 *
 * In a single `doc.transact`: locate `tableAnchor(label, id)` in `doc.getText('content')`
 * and delete that range, then `deleteTable(doc, id)`. A missing anchor is tolerated (the
 * model is still dropped — orphan cleanup); a missing model is a no-op. Precedent for
 * editing the content Y.Text directly (not via a CM6 view): table-reconcile.ts.
 *
 * INVARIANT: same ydoc as the inline editor's yCollab-bound `content` — Yjs merges the
 * delete correctly; the editor re-renders the now-empty anchor range. One widget action =
 * one atomic Yjs update (mirrors the model-helper write-path INVARIANT above).  Why: the delete runs against the same ydoc as the editor's content binding so Yjs merges it correctly and the editor re-renders the empty anchor; one widget action = one atomic update.
 */
export function deleteTableFromDoc(doc: Y.Doc, tableId: string, label: string): void {
  const content = doc.getText('content');
  const text = content.toString();
  const needle = tableAnchor(label, tableId);
  const at = text.indexOf(needle);
  doc.transact(() => {
    if (at >= 0) content.delete(at, needle.length);
    deleteTable(doc, tableId);
  });
}

/**
 * Delete a whole table WITH a backup recovery point (References-badge delete path).
 *
 * Awaits `backup()` (a forced checkpoint POST — captures the LIVE server ydoc while the
 * table is still present) BEFORE `deleteTableFromDoc` mutates the map. `backup` is injected
 * (the model layer stays free of the api/i18n/store coupling the caller owns).
 *
 * INVARIANT(data-loss): the backup is awaited and must RESOLVE before any mutation; if it
 * REJECTS the delete is aborted (the rejection propagates, the model is left intact). Why:
 * a table delete with no recovery point is unrecoverable user-data loss, and a "backup"
 * that ran after (or in parallel with) the delete could capture the already-deleted state
 * (No silent degradation — the caller surfaces the error).
 */
export async function deleteTableWithBackup(
  doc: Y.Doc,
  tableId: string,
  label: string,
  backup: () => Promise<void>,
): Promise<void> {
  await backup();
  deleteTableFromDoc(doc, tableId, label);
}

/**
 * Sanitize a user-typed table label so it can never break the `![label](table:id)`
 * anchor grammar. `]` ends the alt-text (`[^\]]*` in `TABLE_ANCHOR_RE`) and a newline
 * would split the anchor across two lines; both are neutralized. Applied at the write
 * path (renameTable) so a rename can never produce an unparseable anchor.
 *
 * INVARIANT: the output contains no `]` and no newline. Why: an anchor with either is
 * either unparseable or parses to a truncated label — a silent corruption of `content`.
 * `(`, `[`, spaces, unicode all survive (they do not break the grammar).
 */
export function normalizeTableLabel(raw: string): string {
  return raw.replace(/]/g, '').replace(/\n/g, ' ').trim();
}

/**
 * Rename a table's anchor LABEL (the References-panel "table badge" rename path).
 *
 * In a single `doc.transact`: store the normalized label on the model (fallback, see
 * `setModelLabel`) AND — when the table is LINKED — replace its `![oldLabel](table:id)`
 * anchor range in `content` with the renamed anchor. An UNLINKED table (anchor absent) is
 * still renamed via the model label alone, so an orphan table can be named and keep that
 * name when it is re-inserted. Same direct-content-edit precedent as `deleteTableFromDoc`.
 *
 * INVARIANT: the new label is normalized first, so the resulting anchor always re-parses
 * via `TABLE_ANCHOR_RE`. Why: a typed `]`/newline would otherwise corrupt the anchor
 * (see `normalizeTableLabel`). One widget action = one atomic Yjs update (mirrors the
 * model-helper write-path INVARIANT).
 */
export function renameTable(doc: Y.Doc, tableId: string, oldLabel: string, newLabel: string): void {
  const normalized = normalizeTableLabel(newLabel);
  const content = doc.getText('content');
  const needle = tableAnchor(oldLabel, tableId);
  const at = content.toString().indexOf(needle);
  doc.transact(() => {
    setModelLabel(doc, tableId, normalized);
    if (at >= 0) {
      content.delete(at, needle.length);
      content.insert(at, tableAnchor(normalized, tableId));
    }
  });
}

/**
 * Re-insert a table's transclusion anchor into `content` so a broken/unlinked table becomes
 * reachable in the document again. Appends `\n\n![label](table:id)\n` at the end of the
 * content when no view offset is given; the label is seeded from the model fallback (see
 * `setModelLabel`). One atomic Yjs update. Callers with an editor view should instead insert
 * at the cursor (see ReferencesPanel) — this is the no-view fallback.
 */
export function insertTableAnchor(doc: Y.Doc, tableId: string, label: string): void {
  const content = doc.getText('content');
  const anchor = tableAnchor(normalizeTableLabel(label), tableId);
  doc.transact(() => {
    content.insert(content.length, `\n\n${anchor}\n`);
  });
}

/** One row of `listDocumentTables` — a table badge's identity + shape (no cell text). */
export interface DocumentTableEntry {
  table_id: string;
  label: string;
  rows: number;
  cols: number;
  /**
   * True when the model exists in the ydoc but NO anchor in `content` references it
   * (anchor removed or its `table:id` corrupted). The label lives only in the anchor, so
   * an unlinked table has none (`''`) and cannot be renamed — but its data is intact and
   * it stays reachable (open / delete / restore-by-fixing-the-anchor) from the panel.
   */
  unlinked: boolean;
}

/**
 * Derive the panel list of a document's tables from the LIVE ydoc. Read-only; the pure half
 * of the reactive `useDocumentTables` hook.
 *
 * Two ordered groups:
 *  1. LINKED tables — every `![label](table:id)` anchor in `content` joined with its model's
 *     shape, in document (anchor) order (`unlinked: false`).
 *  2. UNLINKED tables — models present in the ydoc that NO surviving anchor references,
 *     appended AFTER the linked group (`unlinked: true`, empty label).
 *
 * INVARIANT: a model that exists in the ydoc is ALWAYS listed (linked or unlinked) — a table
 * is never hidden from the panel just because its anchor was removed/corrupted. Why: hiding
 * it makes intact data unreachable (the user's only handle to open/delete/restore it is this
 * list); the earlier "orphan model excluded" behavior meant a single mangled char in an
 * anchor visually erased a still-present table. Orphan ANCHORS (anchor, no model) are still
 * excluded (nothing to show); a stray duplicate id yields only its first occurrence.
 */
export function listDocumentTables(doc: Y.Doc): DocumentTableEntry[] {
  const content = doc.getText('content').toString();
  const seen = new Set<string>();
  const out: DocumentTableEntry[] = [];
  for (const m of content.matchAll(TABLE_ANCHOR_RE)) {
    const label = m[1] as string;
    const id = m[2] as string;
    if (seen.has(id)) continue;
    const shape = readTableShape(doc, id);
    if (!shape) continue;
    seen.add(id);
    out.push({ table_id: id, label, rows: shape.rows, cols: shape.cols, unlinked: false });
  }
  // Unlinked models (no anchor references them) — appended below the linked group.
  for (const id of getTablesMap(doc).keys()) {
    if (seen.has(id)) continue;
    const shape = readTableShape(doc, id);
    if (!shape) continue;
    seen.add(id);
    out.push({ table_id: id, label: getModelLabel(doc, id), rows: shape.rows, cols: shape.cols, unlinked: true });
  }
  return out;
}
