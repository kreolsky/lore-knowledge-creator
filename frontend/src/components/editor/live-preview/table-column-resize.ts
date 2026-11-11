/**
 * Table column resize (see SYSTEM: table-block in `table-block-widget.ts`): the column-border
 * and whole-table scale drags, one undo step each, and their double-click fits.
 */
import type { EditorView } from '@codemirror/view';
import type * as Y from 'yjs';
import { beginUndoGroup } from '../../../collab/document-undo';
import { caretHold } from '../thick-cursor';
import { startObjectDrag } from './widgets';
import { documentTextWidth } from './document-width';
import {
  orderedColIds,
  readTableModel,
  readColumnWidths,
  setColumnWidth,
  setColumnWidths,
  type ColumnWidth,
  type ColId,
} from './table-block-model';
import { scaleColumnWidths, fitColumnWidths, MIN_COLUMN_WIDTH, FIT_COLUMN_MAX_SHARE } from './table-widths';
import { cellTextNow, liveOrder } from './table-cell-actions';

/** The widget state a resize reads, each read at gesture time. */
export interface ResizeHost {
  tableId: string;
  doc: () => Y.Doc | null;
  view: () => EditorView | null;
  canEdit: () => boolean;
  /** The widget's closer of the drag in progress; null when none. */
  endResize: () => (() => void) | null;
  setEndResize: (end: (() => void) | null) => void;
}

type NextWidths = (base: ColumnWidth[], dx: number) => ColumnWidth[];

/** The table's edge moved with no CM6 change: the document's caret must be redrawn. */
export function redrawCaret(view: EditorView | null): void {
  view?.dispatch({ effects: caretHold.of(false) });
}

/**
 * Shared column-border / scale-control drag: `next` maps the drag-start widths and the
 * cursor delta to the whole width array, so dragging back restores the start exactly.
 * WHY read on the first MOVE, not on mousedown: a double-click must write only its fit.
 * WHY the caret is held for the drag: it may stand at the table's edge, which moves under
 * it; it comes back at the new edge on release.
 */
function startResize(e: PointerEvent, host: ResizeHost, next: NextWidths): void {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  if (!host.canEdit()) return;
  const startX = e.clientX;
  // Keeps the moved edges lit (index.css) while the pointer leaves the control mid-drag.
  const control = e.currentTarget as HTMLElement;
  control.classList.add('cm-table-block-resize--active');
  let base: ColumnWidth[] | null = null;
  let held = false;
  let ended = false;
  let endGroup = () => {};
  // INVARIANT: every drag that opened an undo group closes it — on the handle's own
  // release, on a release anywhere in the window, and when this widget drops its DOM.
  // Why: a handle detached mid-drag (a peer removes the column or swaps the header row,
  // CM6 evicts the widget) never receives its pointerup; the open group would turn every
  // later edit in the document into ONE Ctrl+Z step until reload.
  const end = () => {
    if (ended) return;
    ended = true;
    window.removeEventListener('pointerup', end, true);
    window.removeEventListener('pointercancel', end, true);
    if (host.endResize() === end) host.setEndResize(null);
    document.body.style.cursor = '';
    control.classList.remove('cm-table-block-resize--active');
    endGroup();
    if (held) redrawCaret(host.view());
  };
  host.endResize()?.();
  host.setEndResize(end);
  window.addEventListener('pointerup', end, true);
  window.addEventListener('pointercancel', end, true);
  document.body.style.cursor = 'col-resize';
  startObjectDrag(e, {
    onStart: () => {
      if (ended) return;
      held = true;
      const doc = host.doc();
      // The whole drag is ONE undo step, however long it pauses.
      if (doc) endGroup = beginUndoGroup(doc);
      host.view()?.dispatch({ effects: caretHold.of(true) });
      base = doc ? readColumnWidths(doc, host.tableId) : null;
    },
    onMove: (ev) => {
      const doc = host.doc();
      if (!ended && doc && base) setColumnWidths(doc, host.tableId, next(base, ev.clientX - startX));
    },
    onEnd: end,
    onCancel: end,
  });
}

/** WHY the mousedown too: a prevented pointerdown still fires it, and the press would
 * reach the cell, or start a text selection. */
function bindResize(el: HTMLElement, host: ResizeHost, next: NextWidths): void {
  el.addEventListener('pointerdown', (e) => startResize(e, host, next));
  el.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
}

/** The whole-table scale control: drag scales every column; double-click fits the table. */
export function bindScaleControl(control: HTMLElement, host: ResizeHost): void {
  bindResize(control, host, (base, dx) => {
    const scaled = scaleColumnWidths(base.map((c) => c.w), base.reduce((sum, c) => sum + c.w, 0) + dx);
    return base.map((c, i) => ({ id: c.id, w: scaled[i] }));
  });
  // Double-click = full document width, columns proportional to their content. Written as
  // px: a one-off fit to the clicker's document width, not a width kept on every screen.
  control.addEventListener('dblclick', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const doc = host.canEdit() ? host.doc() : null;
    const model = doc ? readTableModel(doc, host.tableId) : null;
    const colIds = doc ? orderedColIds(doc, host.tableId) : null;
    const view = host.view();
    const total = view ? documentTextWidth(view) : null;
    if (doc && model && colIds && total) {
      const fit = fitColumnWidths(model.rows, total);
      setColumnWidths(doc, host.tableId, colIds.map((id, i) => ({ id, w: fit[i] })));
      redrawCaret(view);
    }
  });
}

/** A header cell's column-border handle: drag resizes the column; double-click fits it. */
export function bindColumnHandle(handle: HTMLElement, host: ResizeHost, colId: ColId, header: HTMLElement): void {
  bindResize(handle, host, (base, dx) => (
    base.map((c) => (c.id === colId ? { id: c.id, w: Math.max(MIN_COLUMN_WIDTH, c.w + dx) } : c))
  ));
  // Double-click = fit this column to its content (one width write, one undo step).
  handle.addEventListener('dblclick', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const doc = host.doc();
    const width = doc && host.canEdit() ? contentFitWidth(doc, host.tableId, host.view(), colId, header) : null;
    if (doc && width !== null) {
      setColumnWidth(doc, host.tableId, colId, width);
      redrawCaret(host.view());
    }
  });
}

/**
 * The px width that fits column `colId` to its content: the widest line of its raw cell
 * texts (all rows) measured in the header cell's font, plus that cell's horizontal
 * padding, clamped to `[MIN_COLUMN_WIDTH, FIT_COLUMN_MAX_SHARE × document text width]`.
 * WHY client metrics are allowed: the text-length rule binds BIRTH widths only; this is a
 * user resize. Raw markdown measures wider than its rendered link — the column comes out
 * a little wide, the user drags it back. Null when nothing is laid out to measure.
 */
function contentFitWidth(doc: Y.Doc, tableId: string, view: EditorView | null, colId: ColId, header: HTMLElement): number | null {
  const order = liveOrder(doc, tableId);
  const total = view ? documentTextWidth(view) : null;
  const ctx = document.createElement('canvas').getContext('2d');
  if (!order || !total || !ctx) return null;
  // WHY: the th has padding 0; the text padding lives on the mounted editor's .cm-content
  // or on the lazy placeholder (index.css `.cm-table-block th`).
  const host = header.querySelector('.cm-content') ?? header.querySelector('.cm-table-block-cell-editor') ?? header;
  const style = getComputedStyle(host);
  ctx.font = style.font || `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
  let widest = 0;
  for (const rowId of order.rowIds) {
    for (const line of cellTextNow(doc, tableId, rowId, colId).split('\n')) {
      widest = Math.max(widest, ctx.measureText(line).width);
    }
  }
  const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  const cap = Math.floor(FIT_COLUMN_MAX_SHARE * total);
  // WHY +1: under border-collapse the cell's box is ~1px narrower than its column (half of
  // each side border); without it the measured longest line wraps right after the fit.
  return Math.min(Math.max(Math.ceil(widest + padding) + 1, MIN_COLUMN_WIDTH), Math.max(cap, MIN_COLUMN_WIDTH));
}
