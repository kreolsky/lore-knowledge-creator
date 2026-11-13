/**
 * Table cell selection mode (see SYSTEM: table-block in `table-block-widget.ts`): the
 * selection state, its paint, the press/drag gestures and the key actions. Range math and
 * the key table live in `table-cell-selection.ts`.
 */
import type { EditorView } from '@codemirror/view';
import type * as Y from 'yjs';
import { documentUndo } from '../../../collab/document-undo';
import { wireCellCaret } from './table-cell-undo';
import { selectImageAt } from './widgets';
import { openNoteThread } from './link-types';
import { clearCells, cellKey, type MergeArea, type RowId, type ColId } from './table-block-model';
import { tabTarget, type CellAddr, type CellOrder } from './table-cell-actions';
import {
  areaAt,
  cellsIn,
  moveHead,
  NATIVE_KEY_ACTIONS,
  sameCell,
  selectionKeyAction,
  selectionRect,
  type CellSelection,
  type SelectionAction,
} from './table-cell-selection';

interface Point { x: number; y: number }

/** The widget state selection mode reads, each read at call time. */
export interface SelectionHost {
  tableId: string;
  wrap: () => HTMLElement | null;
  doc: () => Y.Doc | null;
  canEdit: () => boolean;
  /** The rendered `rowIds`/`colIds` — the cells a selection paints. */
  order: () => CellOrder;
  /** The merged areas over `order` as last rendered. */
  areas: () => readonly MergeArea[];
  /** Last focused cell (focusin); null = none yet. */
  activeCell: () => CellAddr | null;
  cellEl: (key: string) => HTMLElement | undefined;
  cellView: (key: string) => EditorView | undefined;
  ensureCellEditor: (rowId: RowId, colId: ColId) => EditorView | null;
  focusCell: (rowId: RowId, colId: ColId) => void;
  placeCaretAfterTable: () => void;
  repaintLazy: (keys: readonly string[]) => void;
}

export type CellSelectionMode = ReturnType<typeof createCellSelectionMode>;

export function createCellSelectionMode(host: SelectionHost) {
  /** Cell selection mode (table-cell-selection.ts): local UI state, never in Yjs or
   *  awareness; ids only, resolved against the rendered order on every use. */
  let selection: CellSelection | null = null;
  /** Disarms the cross-cell drag listener of the press in progress; null when none. */
  let endCellDrag: (() => void) | null = null;

  /** The focused mounted cell, or null. */
  function focusedCell(): CellAddr | null {
    const at = host.activeCell();
    return at && host.cellView(cellKey(at.rowId, at.colId))?.hasFocus ? at : null;
  }

  /** Every selected cell, row-major; null without a (still valid) selection. */
  function selectedCells(): CellAddr[] | null {
    const order = host.order();
    const rect = selection ? selectionRect(order, selection, host.areas()) : null;
    return rect ? cellsIn(order, rect) : null;
  }

  /**
   * Enter or move selection mode. The wrap holds DOM focus; cells stay placeholders.
   * WHY tabindex only while selecting: a permanent one would make every press inside the
   * table that is not prevented (a viewer's click, the scrollbar) focus the wrap instead
   * of the document.
   */
  function enterSelection(sel: CellSelection): void {
    const wrap = host.wrap();
    if (!wrap || !host.canEdit()) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && active !== wrap && wrap.contains(active)) active.blur();
    if (!setSelection(sel)) return;
    wrap.tabIndex = -1;
    if (document.activeElement !== wrap) wrap.focus({ preventScroll: true });
  }

  /** Store and paint `sel`, head scrolled into view; false (and selection mode left) when
   *  its anchor or head is no longer rendered. */
  function setSelection(raw: CellSelection): boolean {
    if (!selectionRect(host.order(), raw)) { leaveSelection(); return false; }
    const sel = { anchor: toAreaAnchor(raw.anchor), head: toAreaAnchor(raw.head) };
    selection = sel;
    paintSelection();
    host.cellEl(cellKey(sel.head.rowId, sel.head.colId))?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  }

  /** A covered cell is never an anchor/head: it stands for its area's anchor. */
  function toAreaAnchor(cell: CellAddr): CellAddr {
    const order = host.order();
    const area = areaAt(host.areas(), order.rowIds.indexOf(cell.rowId), order.colIds.indexOf(cell.colId));
    return area ? { rowId: area.anchorRowId, colId: area.anchorColId } : cell;
  }

  function leaveSelection(): void {
    if (!selection && !host.wrap()?.hasAttribute('tabindex')) return;
    selection = null;
    paintSelection();
    host.wrap()?.removeAttribute('tabindex');
  }

  function paintSelection(): void {
    for (const el of host.wrap()?.querySelectorAll('.cm-table-cell-selected') ?? []) {
      el.classList.remove('cm-table-cell-selected', 'cm-table-cell-selection-head');
    }
    const sel = selection;
    if (!sel) { hideOverlay(); return; }
    const cells = selectedCells();
    if (!cells) {
      // A peer removed the anchor's or head's row/column: the caret goes after the table,
      // never to the page body where the keyboard reaches nothing.
      const hadFocus = document.activeElement === host.wrap();
      leaveSelection();
      if (hadFocus) host.placeCaretAfterTable();
      return;
    }
    // WHY visible only: a covered cell is display:none, its zero rect would stretch the frame to the origin.
    const els = cells.map((c) => host.cellEl(cellKey(c.rowId, c.colId)))
      .filter((el): el is HTMLElement => el !== undefined && !el.classList.contains('cm-table-cell-covered'));
    for (const el of els) el.classList.add('cm-table-cell-selected');
    // WHY the head: typing, Enter and paste land in the head cell — the hatch marks it.
    const headEl = host.cellEl(cellKey(sel.head.rowId, sel.head.colId));
    headEl?.classList.add('cm-table-cell-selection-head');
    paintOverlay(els, headEl);
  }

  /** Overlay layers inside the scroller (they scroll with the table), created on demand. */
  function overlayLayers(): { box: HTMLElement; head: HTMLElement } | null {
    const scroller = host.wrap()?.querySelector<HTMLElement>('.cm-table-block-scroll');
    if (!scroller) return null;
    const find = (cls: string): HTMLElement => {
      const found = scroller.querySelector<HTMLElement>(`:scope > .${cls}`);
      if (found) return found;
      const el = document.createElement('div');
      el.className = cls;
      scroller.appendChild(el);
      return el;
    };
    return { box: find('cm-table-selection-box'), head: find('cm-table-selection-head') };
  }

  /** Position `el` over the union of `rects`, in the scroller's scrolled coordinates. */
  function placeOver(el: HTMLElement, rects: DOMRect[]): void {
    const scroller = el.parentElement;
    if (!scroller || rects.length === 0) { el.hidden = true; return; }
    const origin = scroller.getBoundingClientRect();
    const left = Math.min(...rects.map((r) => r.left));
    const top = Math.min(...rects.map((r) => r.top));
    const right = Math.max(...rects.map((r) => r.right));
    const bottom = Math.max(...rects.map((r) => r.bottom));
    el.style.left = `${left - origin.left + scroller.scrollLeft}px`;
    el.style.top = `${top - origin.top}px`;
    el.style.width = `${right - left}px`;
    el.style.height = `${bottom - top}px`;
    el.hidden = false;
  }

  /**
   * One block over the selection's bounding rectangle — a merged-cell or otherwise
   * non-rectangular selection gets its bounding box — plus the head's own layer.
   * WHY an overlay, not a per-cell tint: the tint must cover the grid lines between
   * cells too, and the frame must run around the whole block, not each cell.
   */
  function paintOverlay(els: HTMLElement[], headEl: HTMLElement | undefined): void {
    const layers = overlayLayers();
    if (!layers) return;
    placeOver(layers.box, els.map((el) => el.getBoundingClientRect()));
    placeOver(layers.head, headEl ? [headEl.getBoundingClientRect()] : []);
    watchTableSize();
  }

  function hideOverlay(): void {
    for (const el of host.wrap()?.querySelectorAll<HTMLElement>('.cm-table-selection-box, .cm-table-selection-head') ?? []) {
      el.hidden = true;
    }
    sizeWatch?.disconnect();
    sizeWatch = null;
  }

  /** WHY observe: a column resize or a peer's edit reflows cells under a live selection,
   *  and neither repaints it. Disconnected when the selection ends. */
  let sizeWatch: ResizeObserver | null = null;
  function watchTableSize(): void {
    if (sizeWatch) return;
    const table = host.wrap()?.querySelector('table');
    if (!table) return;
    sizeWatch = new ResizeObserver(() => { if (selection) paintSelection(); });
    sizeWatch.observe(table);
  }

  /** Esc in a cell editor: select that cell. False = refused (the key falls through). */
  function selectCell(at: CellAddr): boolean {
    enterSelection({ anchor: at, head: at });
    return selection !== null;
  }

  /** Shift+press on `at`: true when it selected (the press is consumed). */
  function shiftPressSelects(at: CellAddr): boolean {
    if (selection) {
      enterSelection({ anchor: selection.anchor, head: at });
      return true;
    }
    const focused = focusedCell();
    if (focused && sameCell(focused, at)) return false;
    enterSelection({ anchor: focused ?? at, head: at });
    return true;
  }

  /**
   * A plain left press on a cell that is NOT the focused one: nothing focuses, mounts or
   * reveals until the release, which lands in `focusOnRelease`; a drag to another cell
   * turns into a cell selection with no cell ever focused. Right-click and Shift+press stay
   * immediate (the bubble listener / `openMenu`).
   * WHY on release: a press that focused at once revealed the cell's formatting and then
   * collapsed it again when the drag became a cell selection — two layout jumps per drag.
   * WHY capture + stopImmediatePropagation: a mounted cell's own CM6 handler sits below
   * the th/td and would place the caret on press before a bubble listener ever ran.
   */
  function deferPress(e: MouseEvent, key: string, at: CellAddr): void {
    if (e.button !== 0 || e.shiftKey || !host.canEdit()) return;
    const target = e.target as HTMLElement;
    if (target.closest('.cm-table-block-col-resize')) return;
    const focused = focusedCell();
    if (focused && sameCell(focused, at)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    leaveSelection();
    const active = document.activeElement;
    if (active instanceof HTMLElement && host.wrap()?.contains(active)) active.blur();
    // WHY only for a placeholder: a mounted cell opens its note from its own click handler.
    const noteId = !e.altKey && !host.cellView(key)
      ? target.closest<HTMLElement>('.cm-note-anchor')?.dataset.noteId : undefined;
    const down = { x: e.clientX, y: e.clientY };
    armCellDrag(e, at, (up) => focusOnRelease(at, down, up, noteId));
  }

  /**
   * The release of a deferred press inside its own cell: mount, then focus with the text
   * between press and release selected (a caret for a click). WHY both offsets read before
   * focus: an unfocused editor renders like its placeholder, so the press point still maps
   * to the glyph the user pressed; focusing reveals the formatting and moves the text.
   */
  function focusOnRelease(at: CellAddr, down: Point, up: Point, noteId: string | undefined): void {
    const v = host.ensureCellEditor(at.rowId, at.colId);
    if (!v) return;
    // A click on an image selects it whole — the gesture the mounted image widget owns.
    const img = document.elementFromPoint(up.x, up.y)?.closest('.cm-image-embed');
    if (img && v.contentDOM.contains(img) && img === document.elementFromPoint(down.x, down.y)?.closest('.cm-image-embed')) {
      selectImageAt(v, img);
      return;
    }
    const end = v.state.doc.length;
    const anchor = v.posAtCoords(down, false) ?? end;
    const head = v.posAtCoords(up, false) ?? end;
    v.focus();
    v.dispatch({ selection: { anchor, head } });
    if (noteId && anchor === head) openNoteThread(noteId);
  }

  /** This table's cell under the pointer, or null. */
  function cellAtPoint(x: number, y: number): CellAddr | null {
    const el = document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-row-id][data-col-id]');
    const rowId = el?.dataset.rowId as RowId | undefined;
    const colId = el?.dataset.colId as ColId | undefined;
    if (!el || !rowId || !colId || host.cellEl(cellKey(rowId, colId)) !== el) return null;
    return { rowId, colId };
  }

  /**
   * A left press in `from` arms a cross-cell drag: once the held pointer reaches ANOTHER
   * cell of this table the gesture becomes a cell selection and follows the pointer until
   * release; within one cell it stays a text selection. A press inside a mounted cell's
   * existing text selection is a text drag-and-drop — never armed.
   */
  function armCellDrag(e: MouseEvent, from: CellAddr, onClick?: (up: Point) => void): void {
    const view = onClick ? null : host.cellView(cellKey(from.rowId, from.colId));
    if (view?.state.selection.ranges.some((r) => !r.empty)) {
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY }, false);
      if (view.state.selection.ranges.some((r) => !r.empty && pos >= r.from && pos <= r.to)) return;
    }
    endCellDrag?.();
    const drag = new AbortController();
    let selecting = false;
    const move = (ev: MouseEvent) => {
      if (!(ev.buttons & 1)) { drag.abort(); return; }
      const to = cellAtPoint(ev.clientX, ev.clientY);
      if (!to || (!selecting && sameCell(to, from))) return;
      ev.preventDefault();
      if (!selecting) {
        selecting = true;
        enterSelection({ anchor: from, head: to });
      } else if (selection && !sameCell(to, selection.head)) {
        setSelection({ anchor: selection.anchor, head: to });
      }
    };
    document.addEventListener('mousemove', move, { signal: drag.signal });
    document.addEventListener('mouseup', (ev) => {
      drag.abort();
      if (!onClick || selecting) return;
      const to = cellAtPoint(ev.clientX, ev.clientY);
      if (to && sameCell(to, from)) onClick({ x: ev.clientX, y: ev.clientY });
    }, { signal: drag.signal });
    const end = () => drag.abort();
    drag.signal.addEventListener('abort', () => { if (endCellDrag === end) endCellDrag = null; });
    endCellDrag = end;
  }

  /**
   * A keydown on the wrap in selection mode. A handled key never bubbles; an unhandled
   * one does, so the app's window-level hotkeys keep working (the host ignores it —
   * `ignoreEvent`). Typing and Mod-v keep their default: the browser delivers them into
   * the head cell the action focuses.
   */
  function onSelectionKey(e: KeyboardEvent): void {
    const sel = selection;
    if (!sel || e.target !== host.wrap() || e.isComposing) return;
    const action = selectionKeyAction(e);
    if (!action) return;
    e.stopPropagation();
    if (!NATIVE_KEY_ACTIONS.has(action.kind)) e.preventDefault();
    if (!host.canEdit()) { leaveSelection(); return; }
    runSelectionAction(action, sel);
  }

  function runSelectionAction(action: SelectionAction, sel: CellSelection): void {
    const order = host.order();
    const doc = host.doc();
    const { head } = sel;
    switch (action.kind) {
      case 'move': {
        const next = moveHead(order, sel, action.dRow, action.dCol, action.extend, host.areas());
        if (next) setSelection(next); else leaveSelection();
        return;
      }
      case 'tab': {
        const to = doc ? tabTarget(doc, host.tableId, order, head.rowId, head.colId, action.step, false) : null;
        if (to) setSelection({ anchor: to, head: to });
        return;
      }
      case 'edit':
      case 'type':
        // WHY left first: the focus transfer fires the wrap's focusout, which must see no selection.
        leaveSelection();
        host.focusCell(head.rowId, head.colId);
        return;
      case 'paste': {
        // A selected cell is the whole cell: the native paste lands over its whole text.
        leaveSelection();
        const view = host.ensureCellEditor(head.rowId, head.colId);
        view?.focus();
        view?.dispatch({ selection: { anchor: 0, head: view.state.doc.length } });
        return;
      }
      case 'clear':
        clearSelectedCells();
        return;
      case 'undo':
      case 'redo':
        if (doc) documentUndo(doc)?.[action.kind]();
        return;
      case 'exit':
        leaveSelection();
        host.placeCaretAfterTable();
        return;
    }
  }

  /** Delete / Backspace: every selected cell emptied as ONE undo step (images and note
   *  anchors included — a selected cell is the whole cell); the selection stays. */
  function clearSelectedCells(): void {
    const cells = selectedCells();
    const doc = host.doc();
    if (!cells || !doc) return;
    // WHY wired here too: the popped listener repaints this step's placeholders on undo,
    // and it is otherwise wired only by the first cell mount.
    const undoManager = documentUndo(doc);
    if (undoManager) wireCellCaret(undoManager);
    clearCells(doc, host.tableId, cells);
    host.repaintLazy(cells.map((c) => cellKey(c.rowId, c.colId)));
  }

  return {
    onKey: onSelectionKey,
    active: (): boolean => selection !== null,
    leave: leaveSelection,
    /** Rebuilt cells lose their classes; a removed anchor/head row or column ends the selection. */
    repaint(): void { if (selection) paintSelection(); },
    deferPress,
    shiftPressSelects,
    armCellDrag,
    selectCell,
    selectedCells,
    endDrag(): void { endCellDrag?.(); },
    /** The table DOM is dropped: disarm the drag, forget the selection, drop the wrap's tabindex. */
    reset(): void {
      endCellDrag?.();
      selection = null;
      host.wrap()?.removeAttribute('tabindex');
    },
  };
}
