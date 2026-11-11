/**
 * Table cell actions (see SYSTEM: table-block in `table-block-widget.ts`): the context
 * menu's entries and the insert / delete / move / Tab / Backspace writes, as functions over
 * `(doc, tableId, live order)` that return the cell to focus. The widget only focuses.
 */
import type { EditorView } from '@codemirror/view';
import type * as Y from 'yjs';
import { getViewEntity } from '../../../editor/active-editor';
import { useAppStore } from '../../../store/app-store';
import { t } from '../../../i18n';
import type { CellMenuEntry } from './table-cell-menu';
import {
  orderedRowIds,
  orderedColIds,
  readColumnWidths,
  getCellText,
  addRow,
  addColumn,
  removeRow,
  removeColumn,
  moveRow,
  moveColumn,
  type RowId,
  type ColId,
} from './table-block-model';
import { deleteTableWithCheckpoint, tableAnchor } from './table-document-ops';

export interface CellOrder { rowIds: RowId[]; colIds: ColId[] }
export interface CellAddr { rowId: RowId; colId: ColId }
/** A menu write over the click-time order; returns the cell to focus, null = none. */
export type CellOp = (doc: Y.Doc, order: CellOrder) => CellAddr | null;

/** Live row / column order, or null when the table is gone. */
export function liveOrder(doc: Y.Doc | null, tableId: string): CellOrder | null {
  const rowIds = doc ? orderedRowIds(doc, tableId) : null;
  const colIds = doc ? orderedColIds(doc, tableId) : null;
  return rowIds?.length && colIds?.length ? { rowIds, colIds } : null;
}

export function cellTextNow(doc: Y.Doc | null, tableId: string, rowId: RowId, colId: ColId): string {
  return (doc ? getCellText(doc, tableId, rowId, colId)?.toString() : '') ?? '';
}

/** The `[before, after]` neighbours an item lands between after one step `dir` along
 *  `ids`, or null at that edge. */
function moveBounds<T extends string>(ids: T[], id: T, dir: -1 | 1): [T | null, T | null] | null {
  const i = ids.indexOf(id);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= ids.length) return null;
  return dir < 0 ? [ids[i - 2] ?? null, ids[i - 1]] : [ids[i + 1], ids[i + 2] ?? null];
}

type Axis = 'row' | 'col';

/** One axis of the grid, ids widened to `string` so a single helper serves both; the
 *  casts back to the branded id live here and nowhere else. */
interface AxisOps {
  ids(order: CellOrder): string[];
  id(cell: CellAddr): string;
  at(cell: CellAddr, id: string): CellAddr;
  add(doc: Y.Doc, tableId: string, cell: CellAddr, before: string | null, after: string | null): string | null;
  remove(doc: Y.Doc, tableId: string, id: string): void;
  move(doc: Y.Doc, tableId: string, id: string, before: string | null, after: string | null): void;
}

const AXES: Record<Axis, AxisOps> = {
  row: {
    ids: (order) => order.rowIds,
    id: (cell) => cell.rowId,
    at: (cell, id) => ({ ...cell, rowId: id as RowId }),
    add: (doc, tableId, _cell, before, after) => addRow(doc, tableId, before as RowId | null, after as RowId | null),
    remove: (doc, tableId, id) => removeRow(doc, tableId, id as RowId),
    move: (doc, tableId, id, before, after) =>
      moveRow(doc, tableId, id as RowId, before as RowId | null, after as RowId | null),
  },
  col: {
    ids: (order) => order.colIds,
    id: (cell) => cell.colId,
    at: (cell, id) => ({ ...cell, colId: id as ColId }),
    // A new column is born at the clicked column's width; no width = nothing added.
    add: (doc, tableId, cell, before, after) => {
      const width = readColumnWidths(doc, tableId)?.find((c) => c.id === cell.colId)?.w;
      return width ? addColumn(doc, tableId, width, before as ColId | null, after as ColId | null) : null;
    },
    remove: (doc, tableId, id) => removeColumn(doc, tableId, id as ColId),
    move: (doc, tableId, id, before, after) =>
      moveColumn(doc, tableId, id as ColId, before as ColId | null, after as ColId | null),
  },
};

/** Insert a row above / column left (0) or below / right (1) of the clicked cell; focus the
 *  new line's cell in the clicked column / row. */
function insertAlong(tableId: string, axis: Axis, cell: CellAddr, side: 0 | 1): CellOp {
  return (doc, order) => {
    const a = AXES[axis];
    const ids = a.ids(order);
    const id = a.id(cell);
    const i = ids.indexOf(id);
    const before = side ? id : ids[i - 1] ?? null;
    const after = side ? ids[i + 1] ?? null : id;
    const added = a.add(doc, tableId, cell, before, after);
    return added ? a.at(cell, added) : null;
  };
}

/** Move the clicked row ('row') or column ('col') one step; keep the caret in the moved
 *  cell. WHY bounds from the click-time order: a peer may have moved rows since open. */
function moveAlong(tableId: string, axis: Axis, cell: CellAddr, dir: -1 | 1): CellOp {
  return (doc, order) => {
    const a = AXES[axis];
    const bounds = moveBounds(a.ids(order), a.id(cell), dir);
    if (!bounds) return null;
    a.move(doc, tableId, a.id(cell), ...bounds);
    // WHY refocus: the DOM move of the cell's node drops the focused editor's focus.
    return cell;
  };
}

/** Delete the clicked row / column; focus moves to the cell that takes its place (the
 *  next line along the axis — the previous one when the last went). The last remaining
 *  row / column is never deleted. */
function deleteAlong(tableId: string, axis: Axis, cell: CellAddr): CellOp {
  return (doc, order) => {
    const a = AXES[axis];
    const ids = a.ids(order);
    if (ids.length <= 1) return null;
    const i = ids.indexOf(a.id(cell));
    a.remove(doc, tableId, a.id(cell));
    return a.at(cell, ids[i + 1] ?? ids[i - 1]);
  };
}

/**
 * The context menu of the clicked cell: insert / move / delete row / column / table, all
 * acting on that cell. `run` applies an op against the order at RUN time (see the widget's
 * menuTarget); `order` here only decides which entries are disabled.
 */
export function cellMenuEntries(
  tableId: string, order: CellOrder, rowId: RowId, colId: ColId,
  run: (op: CellOp) => void, deleteTable: () => void,
): CellMenuEntry[] {
  const cell: CellAddr = { rowId, colId };
  return [
    { label: t('tableInsertRowAbove'), icon: 'insertRowAbove', run: () => run(insertAlong(tableId, 'row', cell, 0)) },
    { label: t('tableInsertRowBelow'), icon: 'insertRowBelow', run: () => run(insertAlong(tableId, 'row', cell, 1)) },
    { label: t('tableInsertColumnLeft'), icon: 'insertColumnLeft', run: () => run(insertAlong(tableId, 'col', cell, 0)) },
    { label: t('tableInsertColumnRight'), icon: 'insertColumnRight', run: () => run(insertAlong(tableId, 'col', cell, 1)) },
    'separator',
    { label: t('tableMoveRowUp'), icon: 'moveRowUp', disabled: !moveBounds(order.rowIds, rowId, -1),
      run: () => run(moveAlong(tableId, 'row', cell, -1)) },
    { label: t('tableMoveRowDown'), icon: 'moveRowDown', disabled: !moveBounds(order.rowIds, rowId, 1),
      run: () => run(moveAlong(tableId, 'row', cell, 1)) },
    { label: t('tableMoveColumnLeft'), icon: 'moveColumnLeft', disabled: !moveBounds(order.colIds, colId, -1),
      run: () => run(moveAlong(tableId, 'col', cell, -1)) },
    { label: t('tableMoveColumnRight'), icon: 'moveColumnRight', disabled: !moveBounds(order.colIds, colId, 1),
      run: () => run(moveAlong(tableId, 'col', cell, 1)) },
    'separator',
    { label: t('tableDeleteRow'), icon: 'deleteRow', disabled: order.rowIds.length <= 1,
      run: () => run(deleteAlong(tableId, 'row', cell)) },
    { label: t('tableDeleteColumn'), icon: 'deleteColumn', disabled: order.colIds.length <= 1,
      run: () => run(deleteAlong(tableId, 'col', cell)) },
    'separator',
    { label: t('tableDeleteTable'), icon: 'deleteTable', danger: true, run: deleteTable },
  ];
}

/**
 * Tab (+1) / Shift-Tab (−1): the next/previous cell in row-major display order, wrapping
 * row → row. Tab on the bottom-right cell appends a row (editors only); Shift-Tab on the
 * first cell is a no-op. Positions here are a walk over the CURRENT order, never stored.
 */
export function tabTarget(
  doc: Y.Doc, tableId: string, order: CellOrder, rowId: RowId, colId: ColId, step: 1 | -1, canEdit: boolean,
): CellAddr | null {
  const { rowIds, colIds } = order;
  const r = rowIds.indexOf(rowId);
  const c = colIds.indexOf(colId);
  if (r < 0 || c < 0) return null; // a peer removed this cell's row/column
  const flat = r * colIds.length + c + step;
  if (flat < 0) return null;
  if (flat >= rowIds.length * colIds.length) {
    if (!canEdit) return null;
    const added = addRow(doc, tableId);
    return added ? { rowId: added, colId: colIds[0] } : null;
  }
  return { rowId: rowIds[Math.floor(flat / colIds.length)], colId: colIds[flat % colIds.length] };
}

/**
 * Backspace in the first cell of a row whose every cell is empty, caret at offset 0,
 * removes that row (never the header) and lands at the end of the row above. Every
 * other case falls through to the default Backspace (false).
 */
export function backspaceRow(
  doc: Y.Doc, tableId: string, order: CellOrder, cell: EditorView, rowId: RowId, colId: ColId,
  focus: (to: CellAddr) => void,
): boolean {
  const sel = cell.state.selection.main;
  if (!sel.empty || sel.head !== 0 || order.colIds[0] !== colId) return false;
  const r = order.rowIds.indexOf(rowId);
  if (r <= 0) return false;
  if (order.colIds.some((cid) => cellTextNow(doc, tableId, rowId, cid).trim() !== '')) return false;
  // WHY focus first: removing the row destroys this very cell editor.
  focus({ rowId: order.rowIds[r - 1], colId: order.colIds[order.colIds.length - 1] });
  removeRow(doc, tableId, rowId);
  return true;
}

/** The menu's Delete table: the same backed-up delete as the References badge. */
export async function deleteWholeTable(doc: Y.Doc, tableId: string, label: string, view: EditorView | null): Promise<void> {
  const documentId = view ? getViewEntity(view) : null;
  const anchorAt = view ? view.state.doc.toString().indexOf(tableAnchor(label, tableId)) : -1;
  try {
    if (!documentId) throw new Error('table widget has no document entity');
    await deleteTableWithCheckpoint(doc, documentId, tableId, label, t('tableDeleteBackupLabel'));
  } catch (err) {
    console.error('Failed to back up before table delete', err);
    useAppStore.getState().showToast(t('tableDeleteFailed'), 'error');
    return;
  }
  // The caret lands where the table stood. WHY the view captured up front: the delete
  // destroys this widget. The anchor's offset was read before the backup's await.
  if (!view) return;
  view.focus();
  if (anchorAt >= 0) view.dispatch({ selection: { anchor: Math.min(anchorAt, view.state.doc.length) } });
}
