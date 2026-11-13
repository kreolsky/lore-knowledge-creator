/**
 * Table cell selection (see SYSTEM: table-block in `table-block-widget.ts`): the range math
 * over the live order and the key table of selection mode. Pure — `table-cell-selection-mode.ts`
 * holds the selection, paints it and runs the actions.
 *
 * WHY ids, resolved per use: a selection is addressed by `(rowId, colId)` like every cell
 * (the INVARIANT(persisted) in `table-block-model.ts`); an index rectangle is derived from
 * the CURRENT order each time, so a peer's insert never shifts what is selected.
 */
import type { CellAddr, CellOrder } from './table-cell-actions';
import type { MergeArea } from './table-block-model';

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

const touches = (rect: CellRect, a: MergeArea): boolean =>
  a.r0 <= rect.r1 && rect.r0 <= a.r1 && a.c0 <= rect.c1 && rect.c0 <= a.c1;

/** `rect` grown until it contains whole every merged area it touches. */
export function growOverAreas(rect: CellRect, areas: readonly MergeArea[]): CellRect {
  const out = { ...rect };
  for (let grown = true; grown;) {
    grown = false;
    for (const a of areas) {
      if (!touches(out, a) || (a.r0 >= out.r0 && a.r1 <= out.r1 && a.c0 >= out.c0 && a.c1 <= out.c1)) continue;
      out.r0 = Math.min(out.r0, a.r0); out.r1 = Math.max(out.r1, a.r1);
      out.c0 = Math.min(out.c0, a.c0); out.c1 = Math.max(out.c1, a.c1);
      grown = true;
    }
  }
  return out;
}

/** The merged area holding index cell `(r, c)`, if any. */
export function areaAt(areas: readonly MergeArea[], r: number, c: number): MergeArea | undefined {
  return areas.find((a) => r >= a.r0 && r <= a.r1 && c >= a.c0 && c <= a.c1);
}

/** The selection's rectangle, grown over the merged areas it touches; null when a peer
 *  removed the anchor's or head's row/column. */
export function selectionRect(order: CellOrder, sel: CellSelection, areas: readonly MergeArea[] = []): CellRect | null {
  const a = indexOf(order, sel.anchor);
  const h = indexOf(order, sel.head);
  if (!a || !h) return null;
  return growOverAreas(
    { r0: Math.min(a[0], h[0]), r1: Math.max(a[0], h[0]), c0: Math.min(a[1], h[1]), c1: Math.max(a[1], h[1]) },
    areas,
  );
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
export function moveHead(
  order: CellOrder, sel: CellSelection, dRow: number, dCol: number, extend: boolean, areas: readonly MergeArea[] = [],
): CellSelection | null {
  const h = indexOf(order, sel.head);
  if (!h) return null;
  // A merged area steps as one cell: leave it from its far edge; a covered landing cell
  // maps to its area's anchor.
  const from = areaAt(areas, h[0], h[1]);
  const fr = from ? (dRow > 0 ? from.r1 : dRow < 0 ? from.r0 : h[0]) : h[0];
  const fc = from ? (dCol > 0 ? from.c1 : dCol < 0 ? from.c0 : h[1]) : h[1];
  let r = Math.min(Math.max(fr + dRow, 0), order.rowIds.length - 1);
  let c = Math.min(Math.max(fc + dCol, 0), order.colIds.length - 1);
  const to = areaAt(areas, r, c);
  if (to) [r, c] = [to.r0, to.c0];
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
