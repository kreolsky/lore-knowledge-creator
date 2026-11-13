/**
 * Editable table block — CM6 inline replace-decoration widget.
 *
 * SYSTEM: table-block — frontend render entry point. Replaces a `![label](table:id)`
 * transclusion anchor with a table whose CELLS are each a nested, editable CM6 EditorView
 * bound (via `createYjsExtension`) to that cell's per-cell `Y.Text` in the doc-local
 * `tables` Yjs subtree (see `table-block-model.ts`).
 *
 * ARCH: each cell is a nested EditorView running `editableCellExtensions()` — the SAME
 * live-preview markdown rendering as the document, plus the native thick caret. yCollab keeps each cell's content
 * synced to its Y.Text, so the widget itself only manages table STRUCTURE
 * (rows/columns/widths), never cell text.
 *
 * ARCH (id-keyed DOM): every row `tr`, `col` and cell is keyed by the model's ids
 * (`data-row-id`/`data-col-id`, cell maps keyed `cellKey(rowId, colId)`), and a structural
 * change is a PATCH: an inserted row/column creates only its cells, a removed one destroys
 * only its cells, a moved one is one DOM move per row (`reconcileChildren`). Every other
 * cell — including a focused, mounted cell editor — survives a peer's add/remove/move.
 *
 * INVARIANT: the widget creates or destroys cell editors ONLY for the rows/columns a
 * structural change added or removed (plus the old and new header rows when the first
 * row changes — `th` vs `td`). Content edits flow through yCollab in place.  Why: rebuilding
 * cell editors on every keystroke would destroy the focused editor (cursor/selection lost);
 * only structural changes touch cells, and only the cells they name. The observer watches
 * the whole `tables` map (so a model that syncs in after the anchor still mounts), but
 * `render()` returns at once when the ordered ids are unchanged (the model's order cache
 * hands back the same arrays until an axis changes, so a keystroke costs two identity
 * checks).
 *
 * INVARIANT: the live Y.Doc is resolved by the view's ENTITY, never the focused slot
 * (getViewEntity → subscribeEntityHandle; hosts without a `viewEntity` provider get a
 * one-shot slot read). Why: the slot follows FOCUS, so in split view at read time it is
 * null or the OTHER column's handle — binding it renders the wrong ydoc; identity is the
 * only correct address here. A null handle (mount race) renders a transient empty state,
 * never a crash. The subscription lives until destroy(): the first non-null ENTITY handle
 * binds AND a REPLACED handle re-binds (a widget left bound to the old ydoc renders stale
 * and its cell edits stop reaching peers).
 *
 * INVARIANT (snapshot preview): the doc source is `state.facet(tableDocSource)` first,
 * falling back to the live handle ONLY when no facet is provided. A snapshot-preview
 * Editor instance provides a preview-local Y.Doc seeded from the checkpoint's
 * `tables_json`, so the preview renders THAT captured table state — never the live
 * editor's. When `state.readOnly` is true (preview), the widget renders STATIC cells:
 * no nested cell EditorViews, no column-resize handles, no cell menu, and no
 * `claimNestedView` — every write path is closed so a preview can never mutate state.
 * A viewer or commentator has `readOnly` false: their gate is the editable facet (`canEdit`).
 */
import { WidgetType, EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import * as Y from 'yjs';
import { subscribeEntityHandle } from '../../../collab/active-handle-registry';
import { claimNestedView, getActiveHandle, getEditorView, getViewEntity } from '../../../editor/active-editor';
import { selectionFeedPlugin } from '../../../editor/editor-observer';
import { documentUndo } from '../../../collab/document-undo';
import { registerCellCaretHost, wireCellCaret, type CellCaret } from './table-cell-undo';
import { canEditView } from './widgets';
import { useNoteChatStore } from '../../../store/note-chat-store';
import { openNoteThread } from './link-types';
import { atomicRangeAt } from './atomic-delete-guard';
import { tableDocSource } from './table-doc-source';
import { renderCellStatic, isPlainCellText } from './cell-static-render';
import {
  setCellCurrentMatch,
  setCellMatches,
  type CurrentCellMatch,
  type MatchRange,
} from '../find-matches';
import { ESTIMATED_ROW_HEIGHT, TABLE_WRAP_PADDING } from './table-widget';
import { openCellMenu } from './table-cell-menu';
import { t } from '../../../i18n';
import {
  getTablesMap,
  tableAwaitsMigration,
  orderedRowIds,
  orderedColIds,
  readColumnWidths,
  getCellText,
  ensureCell,
  cellKey,
  coveredCells,
  resolveMerges,
  type MergeArea,
  type ColumnWidth,
  type RowId,
  type ColId,
} from './table-block-model';
import { deleteTableFromDoc } from './table-document-ops';
import { reconcileChildren, sameIds } from './dom-reconcile';
import { lazyCellMounters, takeTableFocus, widgetsByWrap } from './table-widget-registry';
import { cellEditorExtensions } from './table-cell-editor';
import { bindColumnHandle, bindScaleControl, type ResizeHost } from './table-column-resize';
import {
  backspaceRow,
  cellMenuEntries,
  deleteWholeTable,
  liveOrder,
  tabTarget,
  type CellOp,
  type CellOrder,
} from './table-cell-actions';
import { sameCell } from './table-cell-selection';
import { createCellSelectionMode, type CellSelectionMode } from './table-cell-selection-mode';
import { createPlaceholderPainter, type CellSlot, type PlaceholderPainter } from './table-cell-placeholders';

/**
 * Markdown cells rendered synchronously when a table is built; the rest are upgraded from
 * raw text in time slices after the table is on screen. Plain-text cells never count (they
 * cost a text node). WHY a budget: a markdown placeholder costs a CM6 dispatch (~0.3ms), so a
 * large imported table rendered all at once delayed opening the document by seconds; the
 * top of the table (what is on screen first) is formatted immediately.
 */
export const EAGER_MARKDOWN_RENDERS = 200;

export class TableBlockWidget extends WidgetType {
  private unobserve: (() => void) | null = null;
  /** Entity-handle late-bind subscription — dropped once bound or on destroy. */
  private unsubEntity: (() => void) | null = null;
  /** Note-list subscription — repaints noted placeholders; dropped on destroy. */
  private unsubNotes: (() => void) | null = null;
  /** Registration as a cell-caret host of the undo stack (see table-cell-undo); dropped on destroy. */
  private unregisterCaret: (() => void) | null = null;
  private tableEl: HTMLTableElement | null = null;
  private colgroupEl: HTMLElement | null = null;
  private theadEl: HTMLElement | null = null;
  private tbodyEl: HTMLElement | null = null;
  private boxEl: HTMLElement | null = null;
  private wrap: HTMLElement | null = null;
  private doc: Y.Doc | null = null;
  private view: EditorView | null = null;
  /** True on a read-only host (snapshot preview) → static cells, no write affordances. */
  private readOnly = false;
  /** Row / column ids in the order last rendered (the model's cached arrays). */
  private rowIds: RowId[] = [];
  private colIds: ColId[] = [];
  /** The row rendered as the header (`th` cells) — the first row in order. */
  private headRowId: RowId | null = null;
  private rowEls = new Map<RowId, HTMLTableRowElement>();
  private colEls = new Map<ColId, HTMLTableColElement>();
  /** Every rendered cell, keyed by `cellKey(rowId, colId)`. */
  private cells = new Map<string, CellSlot>();
  /** One nested EditorView per MOUNTED cell, keyed by `cellKey`. Lazily mounted (see ensureCellEditor). */
  private cellViews = new Map<string, EditorView>();
  /** Accepted merged areas as last rendered, their signature, and the cells they styled. */
  private areas: MergeArea[] = [];
  private mergeSig = '';
  private spanned = new Set<string>();
  /** Each header column's resize handle, and those currently placed inside a merged header. */
  private colHandles = new Map<ColId, HTMLElement>();
  private placedHandles = new Map<HTMLElement, ColId>();
  /** Remaining synchronous markdown renders for the table being built. */
  private eagerRendersLeft = 0;
  /** Last focused cell — the undo caret's source; null = none yet. */
  private activeCell: { rowId: RowId; colId: ColId } | null = null;
  /** Closer of the column/scale drag in progress (idempotent); null when none. */
  private endResize: (() => void) | null = null;
  /** Last rendered row count (for `estimatedHeight`, which CM6's height map reads). */
  private renderedRowCount = 2;
  /** Closer of the open cell context menu; null when none. */
  private closeMenu: (() => void) | null = null;
  /** Unmounted cells' placeholder painting (table-cell-placeholders.ts). */
  private readonly painter: PlaceholderPainter;
  /** Cell selection mode (table-cell-selection-mode.ts). */
  private readonly selection: CellSelectionMode;
  constructor(readonly tableId: string, readonly label: string) {
    super();
    this.painter = createPlaceholderPainter({
      tableId: this.tableId,
      view: () => this.view,
      doc: () => this.doc,
      readOnly: () => this.readOnly,
      cells: this.cells,
      cellViews: this.cellViews,
      slotText: (slot) => this.slotText(slot),
    });
    this.selection = createCellSelectionMode({
      tableId: this.tableId,
      wrap: () => this.wrap,
      doc: () => this.doc,
      canEdit: () => this.canEdit(),
      order: () => ({ rowIds: this.rowIds, colIds: this.colIds }),
      areas: () => this.areas,
      activeCell: () => this.activeCell,
      cellEl: (key) => this.cells.get(key)?.el,
      cellView: (key) => this.cellViews.get(key),
      ensureCellEditor: (rowId, colId) => this.ensureCellEditor(rowId, colId),
      focusCell: (rowId, colId) => this.focusCell(rowId, colId),
      placeCaretAfterTable: () => this.placeCaretAfterTable(),
      repaintLazy: (keys) => this.painter.repaintLazy(keys),
    });
  }

  eq(other: TableBlockWidget): boolean {
    return other.tableId === this.tableId && other.label === this.label;
  }

  ignoreEvent(): boolean { return true; }

  /** WHY: the caret stands beside the table, its height, not the wrap's, which also holds
   * its vertical padding. CM6's own hook, so the caret layer needs no table knowledge. */
  coordsAt(dom: HTMLElement, pos: number): { left: number; right: number; top: number; bottom: number } | null {
    const box = dom.querySelector('.cm-table-block-box')?.getBoundingClientRect();
    if (!box) return null;
    const x = pos > 0 ? box.right : box.left;
    return { left: x, right: x, top: box.top, bottom: box.bottom };
  }

  toDOM(view: EditorView): HTMLElement {
    this.view = view;
    const wrap = document.createElement('div');
    wrap.className = 'cm-table-block-wrap';
    wrap.setAttribute('data-table-id', this.tableId);
    this.wrap = wrap;
    widgetsByWrap.set(wrap, this);
    this.unregisterCaret ??= registerCellCaretHost({
      tableId: this.tableId,
      boundDoc: () => this.doc,
      focusedCaret: (changed) => this.focusedCaret(changed),
      restoreCaret: (caret) => this.restoreCaret(caret),
      isVisible: () => !!this.wrap?.isConnected && this.wrap.getClientRects().length > 0,
      repaintChanged: (changed) => this.painter.repaintChanged(changed),
    });
    wrap.addEventListener('mousedown', (e) => this.placeCaretBeside(e));
    wrap.addEventListener('keydown', (e) => this.selection.onKey(e));
    wrap.addEventListener('focusout', (e) => {
      if (e.target !== wrap || !this.selection.active()) return;
      // WHY deferred: a window switch blurs the wrap but leaves it the active element — the
      // selection survives that; focus moving anywhere else ends it.
      setTimeout(() => { if (document.activeElement !== wrap) this.selection.leave(); }, 0);
    });
    // WHY: prefer the facet doc (snapshot preview's captured tables) over the live
    // handle so a preview renders ITS table state, not the editor's current one.
    this.doc = view.state.facet(tableDocSource);
    this.readOnly = view.state.readOnly;

    if (this.doc) {
      this.attachDoc(this.doc);
    } else {
      // ARCH (split view): resolve the view's OWN entity — the focused slot may be
      // null or hold the OTHER column's handle at this moment. Late-bind: subscribe
      // to the entity's handle; the FIRST non-null ENTITY handle binds.
      // INVARIANT: never bind the first non-null SLOT value. Why: in split view the
      // slot routinely holds the primary's handle while this widget lives in the
      // secondary column — binding it renders this table against the wrong ydoc
      // (shape miss → the destructive "remove anchor?" affordance on a healthy
      // table).
      const entityId = getViewEntity(view);
      if (entityId) {
        // WHY: the subscription lives for the WIDGET's lifetime, not just until the
        // first bind — a REPLACED entity handle (re-join landing a new ydoc for the
        // same entity) must re-bind. destroy() drops the subscription.
        this.unsubEntity = subscribeEntityHandle(entityId, (handle) => {
          if (!handle?.ydoc || this.doc === handle.ydoc) return;
          if (this.doc) {
            // Replacement: drop the old map observer AND the DOM built from the old
            // ydoc — its ids name the old doc's rows, so a patch would mix the two.
            this.unobserve?.();
            this.resetTable();
          }
          this.attachDoc(handle.ydoc);
        });
      } else {
        // No `viewEntity` provider (PublicEditor, nested transclusion views): one-shot
        // slot read — blank until a remount if the handle has not landed yet.
        const ydoc = getActiveHandle()?.ydoc ?? null;
        if (ydoc) this.attachDoc(ydoc);
      }
      this.render(); // no-op without a doc; observer attaches via the subscription
    }

    if (takeTableFocus(this.tableId)) {
      requestAnimationFrame(() => {
        const rowId = this.rowIds[0];
        const colId = this.colIds[0];
        if (rowId && colId) this.focusCell(rowId, colId);
      });
    }
    return wrap;
  }

  /**
   * A press on the wrap's empty part (its padding) places the document's caret
   * beside the table: before it from above the box, after it otherwise.
   * WHY by hand: `ignoreEvent` is true, so CM6 drops this press.
   * WHY the whole scroller is excluded: it holds the cells, which take their own presses,
   * and its own scrollbar, whose drag must not move the caret.
   */
  private placeCaretBeside(e: MouseEvent): void {
    const view = this.view;
    if (!view || e.button !== 0 || !this.canEdit()) return;
    if ((e.target as HTMLElement).closest('.cm-table-block-scroll, .cm-image-resize-handle, button')) return;
    const range = atomicRangeAt(view.state, view.posAtDOM(e.currentTarget as HTMLElement));
    if (!range) return;
    e.preventDefault();
    const above = this.boxEl && e.clientY < this.boxEl.getBoundingClientRect().top;
    view.focus();
    view.dispatch({ selection: { anchor: above ? range.from : range.to } });
  }

  /** Editability dropped: mounted cells go back to placeholders. A no-op if it returned. */
  dropCellEditors(): void {
    if (this.canEdit() || this.cellViews.size === 0) return;
    this.resetTable(); // WHY a fresh build: render() otherwise sees the same ids and keeps the dead hosts.
    this.render();
  }

  /** Re-queue every unmounted markdown placeholder for a deferred re-render — its embeds
   *  may resolve differently now (see cellLinkContextRelay). */
  rerenderLazyCells(): void {
    if (!this.doc) return;
    this.painter.requeueMarkdown();
  }

  /** Bind the live ydoc: attach the whole-map observer, then render. */
  private attachDoc(doc: Y.Doc): void {
    this.doc = doc;
    // Watch the WHOLE tables map: the anchor and the model can sync in separate ticks,
    // and structural ops (add/remove/move row/col) must patch the cells.
    const tables = getTablesMap(doc);
    const cb = () => this.render();
    tables.observeDeep(cb);
    this.unobserve = () => tables.unobserveDeep(cb);
    this.unsubNotes ??= useNoteChatStore.subscribe((next, prev) => {
      if (next.sessions !== prev.sessions) this.painter.refreshNotedPlaceholders();
    });
    this.render();
  }

  /** Render (build or patch) the table from the current model. */
  private render(): void {
    const wrap = this.wrap;
    const doc = this.doc;
    if (!wrap || !doc) return; // mount race — observer attaches; re-render later.

    // INVARIANT(data-loss): a v1 table awaiting migration renders an explicit error,
    // never an empty grid. Why: see `tableAwaitsMigration` — an empty table is a lie
    // the user would "fix" by typing into it.
    if (tableAwaitsMigration(getTablesMap(doc).get(this.tableId))) {
      this.resetTable();
      this.renderedRowCount = 0;
      wrap.replaceChildren(this.renderError(t('tableBlockAwaitsMigration')));
      return;
    }

    const rowIds = orderedRowIds(doc, this.tableId);
    const colIds = orderedColIds(doc, this.tableId);
    if (!rowIds || !colIds || rowIds.length === 0) {
      this.resetTable();
      this.renderedRowCount = 0;
      // Readonly (preview): a missing model means the checkpoint did not capture this
      // table's state (legacy) — show a distinct, non-actionable placeholder, not the
      // editable "remove anchor?" affordance (a preview can't edit).
      wrap.replaceChildren(this.readOnly ? this.renderError(t('tableBlockSnapshotMissing')) : this.renderMissing());
      return;
    }

    // Same arrays → a cell-text edit: yCollab already patched it in place. The order cache
    // hands back new arrays on any axis write (a width drag included), so this is exact.
    // Merged areas resolve over the order; a merge/unmerge changes no axis, so its
    // signature is checked beside the arrays.
    const areas = resolveMerges(doc, this.tableId);
    const mergeSig = areas.map((a) => `${a.anchorRowId}:${a.anchorColId}:${a.r1}:${a.c1}:${a.r0}:${a.c0}`).join('|');
    const sameOrder = rowIds === this.rowIds && colIds === this.colIds;
    if (this.tableEl && sameOrder && mergeSig === this.mergeSig) return;

    if (!this.tableEl) this.buildFresh();
    const patched = this.patchStructure(rowIds, colIds);
    const mergesChanged = patched || mergeSig !== this.mergeSig;
    if (mergesChanged) this.applyMerges(areas, mergeSig);
    if (!sameOrder) this.applyColumnWidths(readColumnWidths(doc, this.tableId) ?? []);
    if (mergesChanged || !sameOrder) this.placeHeaderHandles();
    if (!patched) {
      if (mergesChanged) { this.selection.repaint(); this.view?.requestMeasure(); }
      return; // widths-only or merges-only change
    }
    // The menu names rows/columns of the shape it opened on.
    this.closeMenu?.();
    this.renderedRowCount = rowIds.length;
    // A patch may have recreated the current cell's host (a header swap) — re-apply the
    // panel's current cell match; idempotent on untouched cells.
    const current = this.painter.currentMatch();
    if (current) this.painter.applyCurrentCellMatch(current);
    // Rebuilt cells lose their classes; a removed anchor/head row or column ends the selection.
    this.selection.repaint();
    this.painter.scheduleDeferredRenders();
    // New cell hosts lay out over the next frame — re-measure so CM6 reserves the
    // table's real height (see the cellMeasureListener WHY on overflow/clicks).
    requestAnimationFrame(() => this.view?.requestMeasure());
  }

  /**
   * Style the merged areas: the anchor spans its area, every covered cell is hidden
   * (`cm-table-cell-covered`) and loses a mounted editor, focus going to the anchor.
   * WHY hidden, not removed: the id-keyed patch keeps one element per (row, column); a
   * hidden cell leaves the table layout like an absent one.
   */
  private applyMerges(areas: MergeArea[], sig: string): void {
    this.areas = areas;
    this.mergeSig = sig;
    for (const key of this.spanned) {
      const el = this.cells.get(key)?.el as HTMLTableCellElement | undefined;
      if (!el) continue;
      el.colSpan = 1;
      el.rowSpan = 1;
      el.classList.remove('cm-table-cell-covered');
    }
    this.spanned.clear();
    const order = { rowIds: this.rowIds, colIds: this.colIds };
    for (const a of areas) {
      const key = cellKey(a.anchorRowId, a.anchorColId);
      const el = this.cells.get(key)?.el as HTMLTableCellElement | undefined;
      if (!el) continue;
      el.colSpan = a.c1 - a.c0 + 1;
      el.rowSpan = a.r1 - a.r0 + 1;
      this.spanned.add(key);
    }
    const reveal: string[] = [];
    for (const [key, area] of coveredCells(areas, order)) {
      const slot = this.cells.get(key);
      if (!slot) continue;
      slot.el.classList.add('cm-table-cell-covered');
      this.spanned.add(key);
      const view = this.cellViews.get(key);
      if (!view) continue;
      const hadFocus = view.hasFocus;
      this.destroyCellView(view);
      this.cellViews.delete(key);
      slot.host.classList.add('cm-table-block-cell-lazy');
      reveal.push(key);
      if (hadFocus) this.focusCell(area.anchorRowId, area.anchorColId);
    }
    // A destroyed editor leaves its host empty — repaint it as a placeholder for an unmerge.
    this.painter.repaintLazy(reveal);
  }

  /** An empty table skeleton (colgroup/thead/tbody) in its box; `patchStructure` then
   *  fills it. */
  private buildFresh(): void {
    this.painter.startBuild(); // WHY: the build may follow a remote sync that resolved pending anchors.
    this.eagerRendersLeft = EAGER_MARKDOWN_RENDERS;
    const table = document.createElement('table');
    table.className = 'cm-table-block';
    this.colgroupEl = table.appendChild(document.createElement('colgroup'));
    this.theadEl = table.appendChild(document.createElement('thead'));
    this.tbodyEl = table.appendChild(document.createElement('tbody'));
    this.tableEl = table;
    this.boxEl = this.buildBox(table);
    this.wrap!.replaceChildren(this.boxEl);
  }

  /**
   * Bring the DOM to `rowIds` × `colIds`: drop the cells of removed rows/columns, build
   * the cells of new ones, and move what moved (`reconcileChildren`). Returns false when
   * neither axis changed (a width write).
   */
  private patchStructure(rowIds: RowId[], colIds: ColId[]): boolean {
    const colsChanged = !sameIds(colIds, this.colIds);
    const rowsChanged = !sameIds(rowIds, this.rowIds);
    this.rowIds = rowIds;
    this.colIds = colIds;
    if (!colsChanged && !rowsChanged) return false;

    const liveRows = new Set<string>(rowIds);
    const liveCols = new Set<string>(colIds);
    for (const rid of [...this.rowEls.keys()]) if (!liveRows.has(rid)) this.dropRow(rid);
    for (const [cid, col] of [...this.colEls]) {
      if (liveCols.has(cid)) continue;
      col.remove();
      this.colEls.delete(cid);
      this.colHandles.delete(cid);
      this.dropCells((slot) => slot.colId === cid);
    }
    // The header is the first row in order: when it changes, the old header's cells must
    // become `td` and the new one's `th` (with resize handles) — both rows are rebuilt.
    const head = rowIds[0];
    if (this.headRowId !== null && this.headRowId !== head) {
      this.dropRow(this.headRowId);
      this.dropRow(head);
    }
    this.headRowId = head;

    reconcileChildren(this.colgroupEl!, colIds.map((cid) => this.colEl(cid)));
    const trs = rowIds.map((rid) => {
      const fresh = !this.rowEls.has(rid);
      const tr = this.rowEl(rid);
      if (fresh || colsChanged) {
        const tag = rid === head ? 'th' : 'td';
        reconcileChildren(tr, colIds.map((cid) => this.cellEl(tag, rid, cid)));
      }
      return tr;
    });
    reconcileChildren(this.theadEl!, trs.slice(0, 1));
    reconcileChildren(this.tbodyEl!, trs.slice(1));
    return true;
  }

  private colEl(colId: ColId): HTMLTableColElement {
    let col = this.colEls.get(colId);
    if (!col) {
      col = document.createElement('col');
      col.dataset.colId = colId;
      this.colEls.set(colId, col);
    }
    return col;
  }

  private rowEl(rowId: RowId): HTMLTableRowElement {
    let tr = this.rowEls.get(rowId);
    if (!tr) {
      tr = document.createElement('tr');
      tr.dataset.rowId = rowId;
      this.rowEls.set(rowId, tr);
    }
    return tr;
  }

  private cellEl(tag: 'th' | 'td', rowId: RowId, colId: ColId): HTMLElement {
    return this.cells.get(cellKey(rowId, colId))?.el ?? this.buildCell(tag, rowId, colId);
  }

  /** Remove one row's `tr` and every cell in it. */
  private dropRow(rowId: RowId): void {
    const tr = this.rowEls.get(rowId);
    if (!tr) return;
    this.dropCells((slot) => slot.rowId === rowId);
    tr.remove();
    this.rowEls.delete(rowId);
  }

  /** Destroy the cells matching `pred`: their mounted editors, hosts and DOM. */
  private dropCells(pred: (slot: CellSlot) => boolean): void {
    for (const [key, slot] of [...this.cells]) {
      if (!pred(slot)) continue;
      const view = this.cellViews.get(key);
      if (view) this.destroyCellView(view);
      this.cellViews.delete(key);
      this.cells.delete(key);
      this.painter.forget(key);
      slot.el.remove();
    }
  }

  /**
   * Horizontal scroller around the TABLE only. WHY: fixed px widths that sum
   * past the document column must scroll INSIDE the
   * block, never widen the page. overflow-y is explicit — with only overflow-x
   * set it computes to auto and adds an inner vertical scrollbar, which breaks
   * widget height measurement. The frame and its controls stay OUTSIDE.
   */
  private buildScroller(table: HTMLTableElement): HTMLElement {
    const scroller = document.createElement('div');
    scroller.className = 'cm-table-block-scroll';
    scroller.appendChild(table);
    return scroller;
  }

  /** The table's frame box: scroller + selection frame + whole-table scale control. The
   * frame sits outside the scroller, which would clip it. */
  private buildBox(table: HTMLTableElement): HTMLElement {
    const box = document.createElement('div');
    box.className = 'cm-table-block-box';
    box.appendChild(this.buildScroller(table));
    if (this.readOnly) return box;
    const frame = document.createElement('div');
    frame.className = 'cm-table-block-frame';
    const control = document.createElement('div');
    control.className = 'cm-image-resize-handle cm-image-resize-corner-top cm-table-block-scale';
    bindScaleControl(control, this.resizeHost());
    frame.appendChild(control);
    box.appendChild(frame);
    return box;
  }

  /** The one width rule: every column is px (no auto columns), table and box are `<sum>px`. */
  private applyColumnWidths(widths: ColumnWidth[]): void {
    const table = this.tableEl;
    if (!table) return;
    for (const { id, w } of widths) {
      const col = this.colEls.get(id);
      if (col) col.style.width = `${w}px`;
    }
    const sum = widths.reduce((total, c) => total + c.w, 0);
    table.style.width = `${sum}px`;
    // WHY the extra px: collapsed outer borders render the table 1px past its columns. A
    // box of exactly `sum` would give every table a scrollbar.
    if (this.boxEl) this.boxEl.style.width = `${sum + 1}px`;
    // WHY min(): a plain px width enters .cm-content's min-content and widens the whole
    // document; the percentage drops that contribution, so the table scrolls inside itself.
    if (this.wrap) this.wrap.style.width = `min(100%, ${sum + 1}px)`;
  }

  /** The cell's live Y.Text, null when it was never created (reads empty). */
  private slotText(slot: CellSlot): Y.Text | null {
    return this.doc ? getCellText(this.doc, this.tableId, slot.rowId, slot.colId) : null;
  }

  /** Build a cell hosting a nested editor bound to the cell's Y.Text. */
  private buildCell(tag: 'th' | 'td', rowId: RowId, colId: ColId): HTMLElement {
    const key = cellKey(rowId, colId);
    const cell = document.createElement(tag);
    cell.dataset.rowId = rowId;
    cell.dataset.colId = colId;
    const ytext = this.doc ? getCellText(this.doc, this.tableId, rowId, colId) : null;
    const text = ytext?.toString() ?? '';
    const marks = this.painter.cellMarksFor(key);
    this.painter.trackMarks(key, marks);

    // INVARIANT (snapshot preview): a read-only host renders the captured cell TEXT
    // statically — no nested EditorView, no focus routing, no resize handle, no
    // claimNestedView. Why: every one of those is a write path (or re-points the shared
    // view registry at a preview cell); a preview is a pure viewer, so all of them are
    // closed here. The text is read from the facet-selected (preview-local) doc.
    if (this.readOnly) return this.buildStaticCell(cell, key, rowId, colId, text, marks);
    this.buildLiveCell(cell, key, rowId, colId, ytext, text, marks);
    if (tag === 'th') cell.appendChild(this.buildColResizeHandle(colId, cell));
    return cell;
  }

  /** Read-only host: the cell's text rendered statically (see buildCell's INVARIANT). */
  private buildStaticCell(
    cell: HTMLElement, key: string, rowId: RowId, colId: ColId, text: string, marks: readonly MatchRange[] | undefined,
  ): HTMLElement {
    const body = document.createElement('div');
    body.className = 'cm-table-block-cell-editor cm-table-block-cell-static';
    // Render through the SAME live-preview pipeline as the editable placeholder
    // (links collapsed, marks, ligatures) — textContent would show the raw
    // `[label](url)` source in a public share / history preview. The renderer is
    // read-only and the fragment is cloned without listeners, so this write-path
    // INVARIANT above still holds.
    body.append(renderCellStatic(text, { marks }));
    cell.appendChild(body);
    // WHY registered: a late embed-map refresh re-renders it (rerenderLazyCells) — a pure
    // static re-render, no mount path (no mousedown, no lazyCellMounters entry).
    this.cells.set(key, { rowId, colId, el: cell, host: body });
    return cell;
  }

  /** Editable host: focus/menu/click routing and the lazy placeholder of a live cell. */
  private buildLiveCell(
    cell: HTMLElement, key: string, rowId: RowId, colId: ColId,
    ytext: Y.Text | null, text: string, marks: readonly MatchRange[] | undefined,
  ): void {
    cell.addEventListener('focusin', () => { this.activeCell = { rowId, colId }; });
    cell.addEventListener('contextmenu', (e) => this.openMenu(e, rowId, colId));

    // INVARIANT: a click ANYWHERE in the cell goes to the cell's editor and NEVER to the
    // outer document. Why: the nested cell editors live inside the outer .cm-content, so a
    // cell mousedown bubbles up and the document editor ALSO handles it — a focus race that
    // drops the caret into the document. stopPropagation shields the outer editor.
    //
    // The cell editor is content-sized (it cannot fill a row made tall by a multi-line
    // SIBLING cell — percentage height does not resolve through a table cell whose row height
    // is content-determined), so a short cell in a tall row has dead <th>/<td> space below its
    // text. Two branches handle a click:
    //   - target inside the cell's OWN editor host (.cm-table-block-cell-editor) → already in
    //     the nested .cm-content, CM6 handles it natively (return).
    //   - target on the cell's dead space (the th/td itself) → route via posAtCoords.
    // WHY: the native branch checks `.cm-table-block-cell-editor`, NOT `.cm-content`.
    // Why: the cell lives INSIDE the outer document's .cm-content, so `closest('.cm-content')`
    // matches the OUTER content for a dead-space click (target = the th) and bails — leaving
    // the click for the outer editor, which stole focus and dropped the caret into the document
    // (the long-standing "click below text does nothing / focus leaks" bug). This focus
    // stability is also what makes the cell's thick caret render — it only draws on focus.
    cell.addEventListener('mousedown', (e) => this.selection.deferPress(e, key, { rowId, colId }), { capture: true });
    cell.addEventListener('mousedown', (e) => {
      const target = e.target as HTMLElement;
      // WHY here: a placeholder is a DOM copy with no CM6 handlers, so a press on its note
      // mark opens the thread from this listener — for a commentator too (no mount below).
      const noteId = e.button === 0 && !e.shiftKey && !e.altKey && !this.cellViews.has(key)
        ? target.closest<HTMLElement>('.cm-note-anchor')?.dataset.noteId : undefined;
      // WHY before stopPropagation: a link in a static cell reaches the host's link handler.
      if (e.button !== 0 || !this.canEdit()) { if (noteId) openNoteThread(noteId); return; }
      e.stopPropagation();
      if (target.closest('.cm-table-block-col-resize')) return;
      // Shift+press on ANOTHER cell (or any cell while selecting) selects; inside the focused
      // cell it stays CM6's native text-selection extend below.
      if (e.shiftKey && this.selection.shiftPressSelects({ rowId, colId })) { e.preventDefault(); return; }
      this.selection.leave();
      this.selection.armCellDrag(e, { rowId, colId });
      // Click inside an ALREADY-MOUNTED live cell editor → let CM6 handle it natively.
      // (The static placeholder also carries `.cm-table-block-cell-editor`, so guard on the
      // editor actually existing — otherwise a first click on a lazy cell would bail here.)
      if (this.cellViews.has(key) && target.closest('.cm-table-block-cell-editor')) return;
      // Dead space of the focused cell (any other cell's plain press is deferPress's) → route.
      const v = this.ensureCellEditor(rowId, colId);
      if (!v) return;
      e.preventDefault();
      const pos = v.posAtCoords({ x: e.clientX, y: e.clientY }, false) ?? v.state.doc.length;
      v.focus();
      v.dispatch({ selection: { anchor: pos } });
    });

    // ARCH (lazy editors): a cell is built as a CHEAP formatted placeholder (cell-static-render,
    // no EditorView of its own); the full nested CM6 editor — yCollab binding,
    // cell-nav keymap, reveal-at-caret — mounts ONLY on first focus (see ensureCellEditor).
    // Why: building N×M EditorViews upfront is the dominant cost of opening a table (each is
    // a CM6 state + compiled extensions + DOM); a viewed-but-unedited dataset (imported CSV)
    // mounts ~1 editor instead of N×M. The placeholder shows formatted markdown (matching the
    // "unfocused cell renders formatted" invariant), refreshed when its cell is rebuilt.
    const editorHost = document.createElement('div');
    editorHost.className = 'cm-table-block-cell-editor cm-table-block-cell-lazy';
    // Past the eager budget a markdown cell shows its raw text until its deferred render.
    const deferred = !isPlainCellText(text) && this.eagerRendersLeft-- <= 0;
    editorHost.append(deferred ? text : renderCellStatic(text, { notes: this.painter.cellNotes(ytext), marks }));
    if (deferred) this.painter.defer(key);
    cell.appendChild(editorHost);
    this.cells.set(key, { rowId, colId, el: cell, host: editorHost });
    lazyCellMounters.set(editorHost, () => (
      this.cells.get(key)?.host === editorHost ? this.ensureCellEditor(rowId, colId) : null
    ));
  }

  /** A header cell's column-border handle (drag resizes, double-click fits). */
  private buildColResizeHandle(colId: ColId, header: HTMLElement): HTMLElement {
    const handle = document.createElement('div');
    handle.className = 'cm-image-resize-handle cm-table-block-col-resize';
    bindColumnHandle(handle, this.resizeHost(), colId, header);
    // The lit edge of a placed handle is its column's boundary inside the anchor cell.
    handle.addEventListener('pointerenter', () => {
      if (handle.dataset.edge) handle.parentElement?.style.setProperty('--placed-edge', handle.dataset.edge);
    });
    this.colHandles.set(colId, handle);
    return handle;
  }

  /**
   * Keep every column of a merged header resizable on its own: the handles of the area's
   * columns move into the anchor cell, each on its column's right boundary; an unmerge or a
   * rebuild puts them back. WHY: a covered header cell is hidden with its handle, and the
   * anchor's own handle would sit on the area's right edge while resizing its first column.
   */
  private placeHeaderHandles(): void {
    // Desired spot of every handle inside a merged header: the anchor cell, and a `left`
    // (null = the default right-edge spot, the area's last column).
    const want = new Map<HTMLElement, { colId: ColId; anchor: HTMLElement; x: number | null }>();
    const head = this.rowIds[0];
    for (const a of this.areas) {
      if (a.r0 !== 0 || a.c1 === a.c0) continue;
      const anchor = this.cells.get(cellKey(head, a.anchorColId))?.el;
      if (!anchor) continue;
      let x = 0;
      for (let c = a.c0; c <= a.c1; c++) {
        const colId = this.colIds[c];
        x += parseFloat(this.colEls.get(colId)?.style.width ?? '') || 0;
        const handle = this.colHandles.get(colId);
        if (handle) want.set(handle, { colId, anchor, x: c === a.c1 ? null : x });
      }
    }
    for (const [handle, colId] of this.placedHandles) {
      if (want.has(handle)) continue;
      const own = this.colHandles.get(colId) === handle && this.headRowId
        ? this.cells.get(cellKey(this.headRowId, colId))?.el : undefined;
      handle.classList.remove('cm-table-block-col-resize--placed');
      handle.style.left = '';
      delete handle.dataset.edge;
      if (own) own.appendChild(handle); else handle.remove();
      this.placedHandles.delete(handle);
    }
    for (const [handle, { colId, anchor, x }] of want) {
      // INVARIANT: a handle is re-parented only when its cell changes, never on a width write.
      // Why: this runs on every pointer move of a width drag; moving the dragged element in
      // the DOM drops its pointer capture and the drag stalls under the cursor.
      if (handle.parentElement !== anchor) anchor.appendChild(handle);
      handle.classList.toggle('cm-table-block-col-resize--placed', x !== null);
      handle.style.left = x === null ? '' : `${x - 5}px`;
      if (x === null) delete handle.dataset.edge; else handle.dataset.edge = `${x}px`;
      if (x !== null && handle.matches(':hover, .cm-table-block-resize--active')) {
        anchor.style.setProperty('--placed-edge', `${x}px`);
      }
      this.placedHandles.set(handle, colId);
    }
  }


  /**
   * Lazily mount a cell's nested CM6 editor, replacing its static placeholder. Returns the
   * existing view if already mounted, or null if the cell/host is gone (structural patch).
   *
   * INVARIANT: focusing a cell binds its editor to the LIVE Y.Text (stale placeholder snaps to current).
   * Why: per-cell Y.Text observers on every placeholder would reintroduce O(cells) wiring; instead the
   * stale-display window is peer edits to an unfocused cell, refreshed when the cell is
   * rebuilt or when it gains focus.
   */
  private ensureCellEditor(rowId: RowId, colId: ColId): EditorView | null {
    // INVARIANT(security): a host that is not editable never mounts a cell editor.
    // Why: a commentator's host has `readOnly` false and a nested editor is editable by
    // default, so a mounted cell accepts typing the server drops and the local mirror keeps.
    if (!this.canEdit()) return null;
    const key = cellKey(rowId, colId);
    const existing = this.cellViews.get(key);
    if (existing) return existing;
    if (!this.doc) return null;
    const host = this.cells.get(key)?.host;
    if (!host) return null; // cell dropped by a structural patch; render() owns the DOM.
    // Mounting IS the first write of a missing cell (the concurrent add-row × add-column
    // intersection) — `ensureCell` creates it outside the undo stack.
    const ytext = ensureCell(this.doc, this.tableId, rowId, colId);
    if (!ytext) return null;
    host.classList.remove('cm-table-block-cell-lazy');
    host.replaceChildren(); // drop the static markdown snapshot
    // The cell types onto the entity's document-wide undo stack (null: a doc with no entity).
    const undoManager = documentUndo(this.doc);
    if (undoManager) wireCellCaret(undoManager);
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: ytext.toString(),
        extensions: cellEditorExtensions({
          cellText: ytext,
          undoManager,
          hostView: () => this.view,
          tabStep: (step) => this.tabStep(rowId, colId, step),
          backspaceRow: (cell) => this.backspaceRow(cell, rowId, colId),
          selectCell: () => this.selection.selectCell({ rowId, colId }),
        }),
      }),
    });
    this.cellViews.set(key, view);
    // The placeholder's find marks move into the live editor: mounting wiped the
    // static copy, and a mounted cell paints marks only from setCellMatches.
    const marks = this.painter.cellMarksFor(key);
    if (marks?.length) view.dispatch({ effects: setCellMatches.of(marks) });
    // The current-match class the placeholder era put on the host element dies
    // with the static copy — the live editor paints its own selected mark.
    host.classList.remove('cm-table-block-cell-current');
    const current = this.painter.currentMatch();
    if (current && current.rowId === rowId && current.colId === colId) {
      view.dispatch({ effects: setCellCurrentMatch.of({ from: current.from, to: current.to }) });
    }
    // A newly-mounted editor lays out over the next frame — re-measure so CM6 reserves the
    // cell's real height (see the cellMeasureListener WHY on overflow/clicks).
    this.view?.requestMeasure();
    return view;
  }

  /** Tab / Shift-Tab from a cell — see `tabTarget`. Always handled (true). */
  private tabStep(rowId: RowId, colId: ColId, step: 1 | -1): boolean {
    const order = liveOrder(this.doc, this.tableId);
    if (!this.doc || !order) return true;
    const to = tabTarget(this.doc, this.tableId, order, rowId, colId, step, this.canEdit());
    if (to) this.focusCell(to.rowId, to.colId);
    return true;
  }

  private focusCell(rowId: RowId, colId: ColId): void {
    const area = coveredCells(this.areas, { rowIds: this.rowIds, colIds: this.colIds }).get(cellKey(rowId, colId));
    if (area) [rowId, colId] = [area.anchorRowId, area.anchorColId];
    const view = this.ensureCellEditor(rowId, colId);
    if (!view) return;
    view.focus();
    view.dispatch({ selection: { anchor: view.state.doc.length } });
  }

  /** The focused mounted cell as an undo caret, when its Y.Text is among `changed`. */
  private focusedCaret(changed: ReadonlyMap<unknown, unknown>): CellCaret | null {
    const at = this.activeCell;
    const view = at ? this.cellViews.get(cellKey(at.rowId, at.colId)) : undefined;
    const ytext = at && view?.hasFocus && this.doc ? getCellText(this.doc, this.tableId, at.rowId, at.colId) : null;
    if (!at || !view || !ytext || !changed.has(ytext)) return null;
    const pos = Y.createRelativePositionFromTypeIndex(ytext, view.state.selection.main.head);
    return { tableId: this.tableId, rowId: at.rowId, colId: at.colId, pos };
  }

  /** Undo/redo of a step typed in a cell: mount it if it is a placeholder, caret back, focus. */
  private restoreCaret(caret: CellCaret): void {
    const view = this.ensureCellEditor(caret.rowId, caret.colId);
    if (!view || !this.doc) return;
    const abs = Y.createAbsolutePositionFromRelativePosition(caret.pos, this.doc);
    const anchor = Math.min(abs?.index ?? view.state.doc.length, view.state.doc.length);
    view.focus();
    view.dispatch({ selection: { anchor }, scrollIntoView: true });
  }

  /**
   * Reach a find match inside this table — the Find panel's Next/Prev landing
   * path. Editable host: mount the cell (if not live) and select the match,
   * without focusing it, AFTER the mount's measure — a freshly mounted editor lays out over the next
   * frame, and a synchronous selection lands the caret at a stale position.
   * Where mounting is refused — viewer/commentator (canEdit false), snapshot
   * preview (static cells) — the table scrolls into view; the static cell's
   * current-mark class rides applyCurrentCellMatch (the relay's fan-out of the
   * panel's setCurrentCellMatch). False = the cell is gone (a peer removed its
   * row/column); the caller skips to the next live match.
   */
  revealCellMatch(rowId: RowId, colId: ColId, from: number, to: number): boolean {
    if (this.canEdit()) {
      const view = this.ensureCellEditor(rowId, colId);
      if (!view) return false;
      const key = cellKey(rowId, colId);
      requestAnimationFrame(() => {
        // Identity re-verify at fire time: a structural patch (or teardown)
        // destroyed this view after arming — never dispatch into it.
        if (this.cellViews.get(key) !== view) return;
        // WHY no view.focus(): the panel keeps focus on its Next/Previous button so
        // the next Enter cycles — a focused cell would take that Enter as a newline
        // typed into the table. The current-match mark shows where the walk landed.
        view.dispatch({ selection: { anchor: from, head: to }, scrollIntoView: true });
      });
      return true;
    }
    this.wrap?.scrollIntoView({ block: 'center' });
    return true;
  }

  /** See the placeholder painter's applyCellMarks — the find relay's entry. */
  applyCellMarks(marks: ReadonlyMap<string, readonly MatchRange[]>): void {
    this.painter.applyCellMarks(marks);
  }

  /** See the placeholder painter's applyCurrentCellMatch — the find relay's entry. */
  applyCurrentCellMatch(pos: CurrentCellMatch | null): void {
    this.painter.applyCurrentCellMatch(pos);
  }

  /** The live editor of a MOUNTED cell, or null — the Find panel's single Replace
   *  goes through it so the edit is an ordinary, Ctrl+Z-undoable cell dispatch. */
  mountedCellView(rowId: RowId, colId: ColId): EditorView | null {
    return this.cellViews.get(cellKey(rowId, colId)) ?? null;
  }


  /** An active cell view about to die hands the slot to its host. WHY the plugin check:
   * a host mid-destroy (document switch) has dropped its plugins — never reactivate it. */
  private destroyCellView(view: EditorView): void {
    if (getEditorView() === view && this.view?.plugin(selectionFeedPlugin)) claimNestedView(this.view);
    view.destroy();
  }

  /** Drop every cell and the table DOM state; the next render() builds fresh. */
  private resetTable(): void {
    this.endResize?.(); // a drag's handle goes with the DOM — see the startResize INVARIANT
    for (const view of this.cellViews.values()) this.destroyCellView(view);
    this.cellViews.clear();
    this.cells.clear();
    this.rowEls.clear();
    this.colEls.clear();
    this.colHandles.clear();
    this.placedHandles.clear();
    this.rowIds = [];
    this.colIds = [];
    this.areas = [];
    this.mergeSig = '';
    this.spanned.clear();
    this.headRowId = null;
    this.painter.reset();
    this.tableEl = null;
    this.colgroupEl = null;
    this.theadEl = null;
    this.tbodyEl = null;
    this.closeMenu?.();
    this.boxEl = null;
    this.selection.reset();
  }


  /** WHY read per gesture: CM6 flips the editable facet without rebuilding an eq() widget,
   * so the `readOnly` captured at mount says nothing about a commentator. */
  private canEdit(): boolean {
    return this.view ? canEditView(this.view) : false;
  }

  /** The widget state a column/scale drag reads, each read at gesture time. */
  private resizeHost(): ResizeHost {
    return {
      tableId: this.tableId,
      doc: () => this.doc,
      view: () => this.view,
      canEdit: () => this.canEdit(),
      endResize: () => this.endResize,
      setEndResize: (end) => { this.endResize = end; },
    };
  }

  /**
   * Right-click on a cell of an editable table: insert / delete row / column / table, all
   * acting on the clicked cell. Shift+right-click keeps the native menu (spellcheck, paste).
   */
  private openMenu(e: MouseEvent, rowId: RowId, colId: ColId): void {
    if (e.shiftKey || !this.canEdit() || !this.doc) return;
    e.preventDefault();
    e.stopPropagation();
    // WHY: macOS Ctrl+click is a left press, then this — the press's deferred focus must not
    // move the caret again under the open menu.
    this.selection.endDrag();
    const order = liveOrder(this.doc, this.tableId);
    if (!order) return;
    // A right-click inside the selection keeps it; the entries still act on the clicked cell.
    const selected = this.selection.selectedCells();
    const inSelection = !!selected?.some((c) => sameCell(c, { rowId, colId }));
    if (!inSelection) this.selection.leave();
    // WHY: the menu acts on this cell — focus it (caret at the click) so the target is visible.
    const cell = inSelection ? null : this.ensureCellEditor(rowId, colId);
    if (cell) {
      const pos = cell.posAtCoords({ x: e.clientX, y: e.clientY }, false) ?? cell.state.doc.length;
      cell.focus();
      cell.dispatch({ selection: { anchor: pos } });
    }
    const run = (op: CellOp) => {
      const target = this.menuTarget(rowId, colId);
      const to = target && this.doc ? op(this.doc, target) : null;
      if (to) this.focusCell(to.rowId, to.colId);
    };
    const deleteTable = () => {
      if (this.doc && this.canEdit()) void deleteWholeTable(this.doc, this.tableId, this.label, this.view);
    };
    this.closeMenu = openCellMenu(e.clientX, e.clientY, cellMenuEntries(
      this.doc, this.tableId, order, rowId, colId, inSelection ? selected : null, run, deleteTable));
  }

  /**
   * The menu's live order when the clicked cell still exists and the user may still edit.
   * WHY null for a stale cell: a peer removed the row/column the user clicked — the row
   * they meant is already gone, so an insert/delete next to "whatever is there now" would
   * act on a row they never chose.
   */
  private menuTarget(rowId: RowId, colId: ColId): CellOrder | null {
    const order = this.canEdit() ? liveOrder(this.doc, this.tableId) : null;
    return order && order.rowIds.includes(rowId) && order.colIds.includes(colId) ? order : null;
  }

  /** Backspace at the start of an empty body row removes it — see `backspaceRow`. */
  private backspaceRow(cell: EditorView, rowId: RowId, colId: ColId): boolean {
    const order = this.canEdit() ? liveOrder(this.doc, this.tableId) : null;
    if (!order || !this.doc) return false;
    return backspaceRow(this.doc, this.tableId, order, cell, rowId, colId, (to) => this.focusCell(to.rowId, to.colId));
  }


  /** The host caret right after the table, host focused. */
  private placeCaretAfterTable(): void {
    const view = this.view;
    if (!view || !this.wrap) return;
    const range = atomicRangeAt(view.state, view.posAtDOM(this.wrap));
    view.focus();
    if (range) view.dispatch({ selection: { anchor: range.to } });
  }

  /** Orphan anchor (no model / broken model) → explicit inline error + remove affordance. */
  private renderMissing(): HTMLElement {
    const box = this.renderError(`${t('tableBlockMissing')} — ${t('tableBlockMissingHint')}`);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cm-table-block-remove';
    btn.textContent = t('tableBlockRemove');
    btn.addEventListener('mousedown', (e) => {
      e.preventDefault();
      if (this.doc && this.canEdit()) deleteTableFromDoc(this.doc, this.tableId, this.label);
    });
    box.appendChild(btn);
    return box;
  }

  /**
   * A non-actionable inline error: the snapshot-preview missing-model state (the
   * checkpoint did not capture this table) and the "awaits migration" state. Distinct
   * from `renderMissing` — no "remove anchor?" button — and a message that names the
   * real cause, so it is visibly different from an empty table (no-silent-degradation).
   */
  private renderError(message: string): HTMLElement {
    const box = document.createElement('div');
    box.className = 'cm-table-block-error';
    const msg = document.createElement('span');
    msg.textContent = message;
    box.appendChild(msg);
    return box;
  }

  destroy(): void {
    this.unobserve?.();
    this.unobserve = null;
    this.unsubEntity?.();
    this.unsubEntity = null;
    this.unsubNotes?.();
    this.unsubNotes = null;
    this.unregisterCaret?.();
    this.unregisterCaret = null;
    // INVARIANT(data-loss) (CM6 eviction/reuse): destroy() MUST drop the cached DOM —
    // CM6 drops a widget's DOM on viewport eviction and REUSES the same instance
    // on scroll-back (eq matches), calling toDOM again on a fresh wrap. With tableEl
    // still set, render() takes the same-ids fast path against the STALE
    // detached node and returns without appending anything — the anchor line renders
    // blank, i.e. the table vanishes. Why: the cache exists to keep cell editors alive
    // across keystrokes (see the structural-patch INVARIANT above); it is only valid
    // while THIS DOM is mounted.
    // `renderedRowCount` is deliberately NOT reset: nulling tableEl alone already forces
    // a fresh build, while estimatedHeight reads renderedRowCount precisely for widgets
    // whose DOM is gone — zeroing it would collapse an evicted table's height estimate to
    // the padding and make the scrollbar jump on scroll-back.
    this.resetTable();
  }

  get estimatedHeight(): number {
    // WHY cached, not readTableModel: CM6's height map invokes this getter
    // (and requestMeasure re-runs it). Materializing the full model (all cell strings) here
    // would allocate O(cells) per pass. The row count is cached at render time and is all the
    // estimate needs; before first render it falls back to 2 rows.
    return this.renderedRowCount * ESTIMATED_ROW_HEIGHT + TABLE_WRAP_PADDING;
  }
}
