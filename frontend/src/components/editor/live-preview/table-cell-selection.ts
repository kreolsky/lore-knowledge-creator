/**
 * Table cell selection (see SYSTEM: table-block in `table-block-widget.ts`): the range math
 * over the live order and the key table of selection mode. Pure — the widget holds the
 * selection, paints it and runs the actions.
 *
 * WHY ids, resolved per use: a selection is addressed by `(rowId, colId)` like every cell
 * (the INVARIANT(persisted) in `table-block-model.ts`); an index rectangle is derived from
 * the CURRENT order each time, so a peer's insert never shifts what is selected.
 */
import type { CellAddr, CellOrder } from './table-cell-actions';

export interface CellSelection { anchor: CellAddr; head: CellAddr }
/** Inclusive index rectangle over a `CellOrder`. */
export interface CellRect { r0: number; r1: number; c0: number; c1: number }

export type SelectionAction =
  | { kind: 'move'; dRow: number; dCol: number; extend: boolean }
  | { kind: 'tab'; step: 1 | -1 }
  | { kind: 'edit' }
  | { kind: 'type' }
  | { kind: 'clear' }
  | { kind: 'undo' }
  | { kind: 'redo' }
  | { kind: 'paste' }
  | { kind: 'exit' };

export const sameCell = (a: CellAddr, b: CellAddr): boolean => a.rowId === b.rowId && a.colId === b.colId;

function indexOf(order: CellOrder, cell: CellAddr): [number, number] | null {
  const r = order.rowIds.indexOf(cell.rowId);
  const c = order.colIds.indexOf(cell.colId);
  return r < 0 || c < 0 ? null : [r, c];
}

/** The selection's rectangle, or null when a peer removed the anchor's or head's row/column. */
export function selectionRect(order: CellOrder, sel: CellSelection): CellRect | null {
  const a = indexOf(order, sel.anchor);
  const h = indexOf(order, sel.head);
  if (!a || !h) return null;
  return { r0: Math.min(a[0], h[0]), r1: Math.max(a[0], h[0]), c0: Math.min(a[1], h[1]), c1: Math.max(a[1], h[1]) };
}

/** Every cell inside `rect`, row-major. */
export function cellsIn(order: CellOrder, rect: CellRect): CellAddr[] {
  const out: CellAddr[] = [];
  for (let r = rect.r0; r <= rect.r1; r++) {
    for (let c = rect.c0; c <= rect.c1; c++) out.push({ rowId: order.rowIds[r], colId: order.colIds[c] });
  }
  return out;
}

/** Step the head by `(dRow, dCol)`, clamped at the edges; without `extend` the anchor
 *  follows (a single-cell selection). Null when the head's row/column is gone. */
export function moveHead(order: CellOrder, sel: CellSelection, dRow: number, dCol: number, extend: boolean): CellSelection | null {
  const h = indexOf(order, sel.head);
  if (!h) return null;
  const r = Math.min(Math.max(h[0] + dRow, 0), order.rowIds.length - 1);
  const c = Math.min(Math.max(h[1] + dCol, 0), order.colIds.length - 1);
  const head = { rowId: order.rowIds[r], colId: order.colIds[c] };
  return { anchor: extend ? sel.anchor : head, head };
}

const move = (dRow: number, dCol: number, extend: boolean): SelectionAction => ({ kind: 'move', dRow, dCol, extend });

/** Selection-mode keys by normalized name (`Mod-` = Ctrl or Cmd, then `Alt-`, `Shift-`). */
const KEY_ACTIONS: Record<string, SelectionAction> = {
  ArrowUp: move(-1, 0, false),
  ArrowDown: move(1, 0, false),
  ArrowLeft: move(0, -1, false),
  ArrowRight: move(0, 1, false),
  'Shift-ArrowUp': move(-1, 0, true),
  'Shift-ArrowDown': move(1, 0, true),
  'Shift-ArrowLeft': move(0, -1, true),
  'Shift-ArrowRight': move(0, 1, true),
  Tab: { kind: 'tab', step: 1 },
  'Shift-Tab': { kind: 'tab', step: -1 },
  Enter: { kind: 'edit' },
  F2: { kind: 'edit' },
  Delete: { kind: 'clear' },
  Backspace: { kind: 'clear' },
  'Mod-z': { kind: 'undo' },
  'Mod-Shift-z': { kind: 'redo' },
  'Mod-y': { kind: 'redo' },
  'Mod-v': { kind: 'paste' },
  Escape: { kind: 'exit' },
};

function keyName(e: KeyboardEvent): string {
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  return `${e.ctrlKey || e.metaKey ? 'Mod-' : ''}${e.altKey ? 'Alt-' : ''}${e.shiftKey ? 'Shift-' : ''}${key}`;
}

/** WHY `Process`: the first keydown of an IME composition — it starts typing like a letter. */
const isTyping = (e: KeyboardEvent): boolean =>
  !e.ctrlKey && !e.metaKey && ([...e.key].length === 1 || e.key === 'Process');

/** The action a keydown in selection mode runs; null = not ours, let it bubble. */
export function selectionKeyAction(e: KeyboardEvent): SelectionAction | null {
  return KEY_ACTIONS[keyName(e)] ?? (isTyping(e) ? { kind: 'type' } : null);
}

/** Actions whose key the browser must still deliver to the cell they focus (no preventDefault). */
export const NATIVE_KEY_ACTIONS: ReadonlySet<SelectionAction['kind']> = new Set(['type', 'paste']);
