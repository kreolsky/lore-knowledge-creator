/**
 * Editable table block — CM6 replace-decoration widget (steps 3-5 + cell-editor pivot).
 *
 * SYSTEM: table-block — frontend render entry point. Replaces a `![label](table:id)`
 * transclusion anchor with a table whose CELLS are each a nested, editable CM6 EditorView
 * bound (via `createYjsExtension`) to that cell's per-cell `Y.Text` in the doc-local
 * `tables` Yjs subtree (see `table-block-model.ts`).
 *
 * ARCH: each cell is a nested EditorView running `editableCellExtensions()` — the SAME
 * live-preview markdown rendering as the document, plus the native thick caret. This
 * replaced the earlier contenteditable cells, which could not render markdown and needed a
 * bespoke caret. yCollab keeps each cell's content synced to its Y.Text, so the widget
 * itself only manages table STRUCTURE (rows/columns/widths), never cell text.
 *
 * INVARIANT: the widget rebuilds its cell editors ONLY on a structural change (row/column
 * count). Content edits flow through yCollab in place — re-rendering on every keystroke
 * would tear down the focused cell's editor.  Why: rebuilding cell editors on every keystroke would destroy the focused editor (cursor/selection lost); only structural changes (row/column count) rebuild — content edits flow via yCollab. The observer watches the whole `tables` map
 * (so a model that syncs in after the anchor still mounts), but `render()` no-ops when the
 * shape is unchanged (only column widths are patched).
 *
 * INVARIANT: the live Y.Doc is resolved by the view's ENTITY, never the focused slot
 * (getViewEntity/tableHostEntity → subscribeEntityHandle; legacy no-entity hosts get a
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
 * no nested cell EditorViews, no column-resize handles, no add/delete toolbar, and no
 * `claimNestedView` — every write path is closed so a preview can never mutate state.
 */
import { WidgetType, EditorView, keymap } from '@codemirror/view';
import { EditorState, Prec, Compartment } from '@codemirror/state';
import { insertNewlineAndIndent } from '@codemirror/commands';
import * as Y from 'yjs';
import { subscribeEntityHandle } from '../../../collab/active-handle-registry';
import { claimNestedView, getActiveHandle, getViewEntity } from '../../../editor/active-editor';
import { createYjsExtension } from '../../../collab/yjs-binding';
import { editableCellExtensions } from '../render-bundle';
import { revealAtCursor } from './effects';
import { tableDocSource, tableHostEntity } from './table-doc-source';
import { renderInlineMarkdown } from './inline-markdown';
import { useUIStore } from '../../../store/ui-store';
import { t } from '../../../i18n';
import {
  getTablesMap,
  readTableModel,
  readTableShape,
  readColumnWidths,
  getCellText,
  setColumnWidth,
  addRow,
  addColumn,
  removeRow,
  removeColumn,
  deleteTable,
  tableAnchor,
  type TableModel,
} from './table-block-model';

const ESTIMATED_ROW_HEIGHT = 38;
const WRAP_PADDING = 8;

// Toolbar icons (vanilla DOM widget → inline SVG). Confluence-style table row/column
// glyphs (fill-based, 16-viewBox), rendered at 15px in currentColor.
const svgIcon = (path: string): string =>
  '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 16 16" ' +
  `fill="currentColor"><path d="${path}"/></svg>`;
const ICON_INSERT_ROW = svgIcon(
  'M11,12l2,0l0,2l-2,0l0,2l-2,0l0,-2l-2,0l0,-2l2,0l0,-2l2,0l0,2Zm-5,2l-5,-0c-0.265,0 -0.52,-0.105 -0.707,-0.293c-0.188,-0.187 -0.293,-0.442 -0.293,-0.707l-0,-10c-0,-0.552 0.448,-1 1,-1c2.577,0 11.423,-0 14,0c0.552,0 1,0.448 1,1c-0,1.916 0,8.084 -0,10c-0,0.552 -0.448,1 -1,1l-1,-0l0,-5l-12,-0l0,3l4,0l0,2Zm-4,-10l0,3l12,0l0,-3l-12,0Z');
const ICON_DELETE_ROW = svgIcon(
  'M13,11.414l-1.586,1.586l1.586,1.586l-1.414,1.414l-1.586,-1.586l-1.586,1.586l-1.414,-1.414l1.586,-1.586l-1.586,-1.586l1.414,-1.414l1.586,1.586l1.586,-1.586l1.414,1.414Zm-7,2.586l-5,-0c-0.265,0 -0.52,-0.105 -0.707,-0.293c-0.188,-0.187 -0.293,-0.442 -0.293,-0.707l-0,-10c-0,-0.552 0.448,-1 1,-1c2.577,0 11.423,-0 14,0c0.552,0 1,0.448 1,1c-0,1.916 0,8.084 -0,10c-0,0.552 -0.448,1 -1,1l-1,-0l0,-5l-12,-0l0,3l4,0l0,2Zm-4,-10l0,3l12,0l0,-3l-12,0Z');
const ICON_INSERT_COL = svgIcon(
  'M12,11l-0,2l2,0l-0,-2l2,0l-0,-2l-2,0l-0,-2l-2,0l-0,2l-2,0l-0,2l2,0Zm2,-5l-0,-5c0,-0.265 -0.105,-0.52 -0.293,-0.707c-0.187,-0.188 -0.442,-0.293 -0.707,-0.293l-10,-0c-0.552,-0 -1,0.448 -1,1c-0,2.577 -0,11.423 0,14c0,0.552 0.448,1 1,1c1.916,0 8.084,0 10,0c0.552,-0 1,-0.448 1,-1l-0,-1l-5,0l-0,-12l3,0l-0,4l2,0Zm-10,-4l3,0l-0,12l-3,0l-0,-12Z');
const ICON_DELETE_COL = svgIcon(
  'M11.414,13l1.586,-1.586l1.586,1.586l1.414,-1.414l-1.586,-1.586l1.586,-1.586l-1.414,-1.414l-1.586,1.586l-1.586,-1.586l-1.414,1.414l1.586,1.586l-1.586,1.586l1.414,1.414Zm2.586,-7l-0,-5c0,-0.265 -0.105,-0.52 -0.293,-0.707c-0.187,-0.188 -0.442,-0.293 -0.707,-0.293l-10,-0c-0.552,-0 -1,0.448 -1,1c-0,2.577 -0,11.423 0,14c0,0.552 0.448,1 1,1c1.916,0 8.084,0 10,0c0.552,-0 1,-0.448 1,-1l-0,-1l-5,0l-0,-12l3,0l-0,4l2,0Zm-10,-4l3,0l-0,12l-3,0l-0,-12Z');

// Toolbar button class strings. The idle variant mirrors IconButton's `baseIdle`; the
// armed variant mirrors IconButton's `filledColors.red` (filled danger) so the armed
// delete confirmation matches the app-wide pattern token-for-token.
const BTN_BASE = 'cm-table-block-btn border-none cursor-pointer flex items-center ' +
  'justify-center transition-all duration-150 w-[22px] h-[22px]';
const BTN_IDLE = `${BTN_BASE} bg-transparent text-text-muted hover:bg-surface3 hover:text-text`;
const BTN_ARMED = `${BTN_BASE} bg-red text-white hover:bg-red hover:text-white`;
/** `data-act` values used to locate the delete buttons across toolbar rebuilds. */
const ACT_DELETE_ROW = 'delete-row';
const ACT_DELETE_COL = 'delete-col';

/** tableId queued by `insertTable` to receive focus when its widget first mounts. */
let pendingFocusTableId: string | null = null;
export function requestTableFocus(tableId: string): void {
  pendingFocusTableId = tableId;
}

export class TableBlockWidget extends WidgetType {
  private unobserve: (() => void) | null = null;
  /** Entity-handle late-bind subscription — dropped once bound or on destroy. */
  private unsubEntity: (() => void) | null = null;
  private tableEl: HTMLTableElement | null = null;
  private toolbarEl: HTMLElement | null = null;
  private wrap: HTMLElement | null = null;
  private doc: Y.Doc | null = null;
  private view: EditorView | null = null;
  /** True on a read-only host (snapshot preview) → static cells, no write affordances. */
  private readOnly = false;
  /** One nested EditorView per cell, keyed by `"row:col"`. Lazily mounted (see ensureCellEditor). */
  private cellViews = new Map<string, EditorView>();
  /** Static placeholder host per cell, keyed by `"row:col"` — swapped for a live editor on focus. */
  private cellHosts = new Map<string, HTMLElement>();
  /** Last focused cell — target for delete-row / delete-column controls. */
  private activeCell = { row: 0, col: 0 };
  /** Last rendered row count (for `estimatedHeight`, which CM6 calls on every measure pass). */
  private renderedRowCount = 2;
  /** Last rendered column count (cheap-shape short-circuit in `render`). */
  private lastColCount = 0;
  // SYSTEM: armed-action — double-click-to-confirm for the table delete buttons. The
  // toolbar is plain DOM (not React), so useArmedAction cannot apply; this mirrors its
  // logic (L24–42) in vanilla form. State lives on the INSTANCE (not buildToolbar's local
  // scope) because buildToolbar is re-invoked on every structural change (incl. collaborative
  // edits), so the armed visual must survive and be re-applied from instance state.
  private armedRow = false;
  private armedCol = false;
  private rowArmTimer: ReturnType<typeof setTimeout> | undefined;
  private colArmTimer: ReturnType<typeof setTimeout> | undefined;
  private static readonly ARM_TIMEOUT = 2000;

  constructor(readonly tableId: string, readonly label: string) {
    super();
  }

  eq(other: TableBlockWidget): boolean {
    return other.tableId === this.tableId && other.label === this.label;
  }

  ignoreEvent(): boolean { return true; }

  toDOM(view: EditorView): HTMLElement {
    this.view = view;
    const wrap = document.createElement('div');
    wrap.className = 'cm-table-block-wrap';
    wrap.setAttribute('data-table-id', this.tableId);
    this.wrap = wrap;
    // WHY: prefer the facet doc (snapshot preview's captured tables) over the live
    // handle so a preview renders ITS table state, not the editor's current one.  Why: a snapshot preview must show the captured tables, not the live editor's current mutation, so the facet doc wins over getActiveHandle().
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
      // table; removeAnchor would cut the ref text while deleteTable no-ops on the
      // doc ydoc).
      const entityId = view.state.facet(tableHostEntity) ?? getViewEntity(view);
      if (entityId) {
        // WHY: the subscription lives for the WIDGET's lifetime, not just until the
        // first bind — a REPLACED entity handle (re-join landing a new ydoc for the
        // same entity) must re-bind. destroy() drops the subscription.
        this.unsubEntity = subscribeEntityHandle(entityId, (handle) => {
          if (!handle?.ydoc || this.doc === handle.ydoc) return;
          if (this.doc) this.unobserve?.(); // replacement: drop the old map observer
          this.attachDoc(handle.ydoc);
        });
      } else {
        // No registered entity (legacy hosts): one-shot slot read exactly as before —
        // blank until a remount if the handle has not landed yet.
        const ydoc = getActiveHandle()?.ydoc ?? null;
        if (ydoc) this.attachDoc(ydoc);
      }
      this.render(); // no-op without a doc; observer attaches via the subscription
    }

    if (pendingFocusTableId === this.tableId) {
      pendingFocusTableId = null;
      requestAnimationFrame(() => this.focusCell(0, 0));
    }
    return wrap;
  }

  /** Bind the live ydoc: attach the whole-map observer, then render. */
  private attachDoc(doc: Y.Doc): void {
    this.doc = doc;
    // Watch the WHOLE tables map: the anchor and the model can sync in separate ticks,
    // and structural ops (add/remove row/col) must rebuild the cell editors.
    const tables = getTablesMap(doc);
    const cb = () => this.render();
    tables.observeDeep(cb);
    this.unobserve = () => tables.unobserveDeep(cb);
    this.render();
  }

  /** Render (build or patch) the table from the current model. */
  private render(): void {
    const wrap = this.wrap;
    if (!wrap || !this.doc) return; // mount race — observer attaches; re-render later.

    // Cheap structural probe FIRST (no cell-text materialization). `observeDeep` fires this on
    // every keystroke in any cell; only a row/column-count change needs a rebuild, and content
    // edits are patched in place by yCollab. Materializing the full model (readTableModel) per
    // keystroke would allocate O(rows×cols) strings every time.
    const shape = readTableShape(this.doc, this.tableId);
    if (!shape || shape.rows === 0) {
      this.teardownCells();
      this.tableEl = null;
      this.renderedRowCount = 0;
      // Readonly (preview): a missing model means the checkpoint did not capture this
      // table's state (legacy) — show a distinct, non-actionable placeholder, not the
      // editable "remove anchor?" affordance (a preview can't edit).
      wrap.replaceChildren(this.readOnly ? this.renderMissingReadonly() : this.renderMissing());
      return;
    }

    // Same shape → widths-only patch; cell text is already synced in place by yCollab.
    if (this.tableEl && shape.rows === this.renderedRowCount && this.lastColCount === shape.cols) {
      const widths = readColumnWidths(this.doc, this.tableId);
      if (widths) this.applyColumnWidths(widths);
      this.syncToolbarWidth(); // column resize changes table width — keep the toolbar pinned
      return;
    }
    // Structural change → rebuild cell editors (materialize the model once here).
    const model = readTableModel(this.doc, this.tableId);
    if (!model) return; // shape said it exists; lost a race — next observe tick retries.
    this.teardownCells();
    this.tableEl = this.buildTable(model);
    this.renderedRowCount = shape.rows;
    this.lastColCount = shape.cols;
    // No toolbar on a read-only host (preview) — the add/delete/resize affordances are
    // all write paths and have no place in a pure viewer.
    if (this.readOnly) {
      wrap.replaceChildren(this.tableEl);
    } else {
      this.toolbarEl = this.buildToolbar();
      wrap.replaceChildren(this.tableEl, this.toolbarEl);
    }
    // New cell editors mount and lay out over the next frame — re-measure so CM6 reserves
    // the table's real height (see the cellActiveViewListener INVARIANT on overflow/clicks).
    requestAnimationFrame(() => {
      this.view?.requestMeasure();
      this.syncToolbarWidth();
    });
  }

  /**
   * Pin the toolbar to the table's RIGHT edge without shrinking the widget wrap.
   * WHY not `width: fit-content` on the wrap: it collapsed the CM6 block widget to zero
   * height (the table vanished). Instead the wrap stays full width and the toolbar is
   * sized to the table's rendered width, so its right-aligned buttons meet the table edge.
   */
  private syncToolbarWidth(): void {
    if (!this.toolbarEl || !this.tableEl) return;
    this.toolbarEl.style.width = `${this.tableEl.offsetWidth}px`;
  }

  private applyColumnWidths(widths: number[]): void {
    const cols = this.tableEl?.querySelectorAll('col');
    if (!cols) return;
    widths.forEach((w, i) => {
      const col = cols[i] as HTMLTableColElement | undefined;
      if (col) col.style.width = `${w}px`;
    });
  }

  private buildTable(model: TableModel): HTMLTableElement {
    const table = document.createElement('table');
    table.className = 'cm-table-block';

    const colgroup = document.createElement('colgroup');
    for (const w of model.columns) {
      const col = document.createElement('col');
      col.style.width = `${w}px`;
      colgroup.appendChild(col);
    }
    table.appendChild(colgroup);

    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    model.rows[0].forEach((_, c) => headRow.appendChild(this.buildCell('th', 0, c)));
    thead.appendChild(headRow);
    table.appendChild(thead);

    if (model.rows.length > 1) {
      const tbody = document.createElement('tbody');
      for (let r = 1; r < model.rows.length; r++) {
        const tr = document.createElement('tr');
        model.rows[r].forEach((_, c) => tr.appendChild(this.buildCell('td', r, c)));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
    }
    return table;
  }

  /** Build a cell hosting a nested editor bound to the cell's Y.Text. */
  private buildCell(tag: 'th' | 'td', row: number, col: number): HTMLElement {
    const cell = document.createElement(tag);
    cell.dataset.row = String(row);
    cell.dataset.col = String(col);

    // INVARIANT (snapshot preview): a read-only host renders the captured cell TEXT
    // statically — no nested EditorView, no focus routing, no resize handle, no
    // claimNestedView. Why: every one of those is a write path (or re-points the shared
    // view registry at a preview cell); a preview is a pure viewer, so all of them are
    // closed here. The text is read from the facet-selected (preview-local) doc.
    if (this.readOnly) {
      const text = this.doc ? (getCellText(this.doc, this.tableId, row, col)?.toString() ?? '') : '';
      const body = document.createElement('div');
      body.className = 'cm-table-block-cell-editor cm-table-block-cell-static';
      body.textContent = text;
      cell.appendChild(body);
      return cell;
    }

    cell.addEventListener('focusin', () => { this.activeCell = { row, col }; });

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
    cell.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.stopPropagation();
      const target = e.target as HTMLElement;
      if (target.closest('.cm-table-block-col-resize')) return;
      const key = `${row}:${col}`;
      // Click inside an ALREADY-MOUNTED live cell editor → let CM6 handle it natively.
      // (The static placeholder also carries `.cm-table-block-cell-editor`, so guard on the
      // editor actually existing — otherwise a first click on a lazy cell would bail here.)
      if (this.cellViews.has(key) && target.closest('.cm-table-block-cell-editor')) return;
      // Static placeholder (lazy) or dead cell space → mount the live editor and route the click.
      const v = this.ensureCellEditor(row, col);
      if (!v) return;
      e.preventDefault();
      const pos = v.posAtCoords({ x: e.clientX, y: e.clientY }, false) ?? v.state.doc.length;
      v.focus();
      v.dispatch({ selection: { anchor: pos } });
    });

    // ARCH (lazy editors): a cell is built as a CHEAP formatted placeholder (one
    // renderInlineMarkdown, no EditorView); the full nested CM6 editor — yCollab binding,
    // cell-nav keymap, reveal-at-caret — mounts ONLY on first focus (see ensureCellEditor).
    // Why: building N×M EditorViews upfront is the dominant cost of opening a table (each is
    // a CM6 state + compiled extensions + DOM); a viewed-but-unedited dataset (imported CSV)
    // mounts ~1 editor instead of N×M. The placeholder shows formatted markdown (matching the
    // "unfocused cell renders formatted" invariant), refreshed on any structural rebuild.
    const ytext = this.doc ? getCellText(this.doc, this.tableId, row, col) : null;
    const editorHost = document.createElement('div');
    editorHost.className = 'cm-table-block-cell-editor cm-table-block-cell-static';
    editorHost.appendChild(renderInlineMarkdown(ytext?.toString() ?? ''));
    cell.appendChild(editorHost);
    this.cellHosts.set(`${row}:${col}`, editorHost);

    if (tag === 'th') {
      const handle = document.createElement('div');
      handle.className = 'cm-table-block-col-resize';
      handle.addEventListener('mousedown', (e) => this.startColResize(e, col, cell));
      cell.appendChild(handle);
    }
    return cell;
  }

  /**
   * Lazily mount a cell's nested CM6 editor, replacing its static placeholder. Returns the
   * existing view if already mounted, or null if the cell/host is gone (structural rebuild).
   *
   * INVARIANT: focusing a cell binds its editor to the LIVE Y.Text (stale placeholder snaps to current).
   * Why: per-cell Y.Text observers on every placeholder would reintroduce O(cells) wiring; instead the
   * stale-display window is peer edits to an unfocused cell, refreshed on the next structural rebuild
   * (row/column change) or when the cell gains focus.
   */
  private ensureCellEditor(row: number, col: number): EditorView | null {
    const key = `${row}:${col}`;
    const existing = this.cellViews.get(key);
    if (existing) return existing;
    if (!this.doc) return null;
    const ytext = getCellText(this.doc, this.tableId, row, col);
    if (!ytext) return null;
    const host = this.cellHosts.get(key);
    if (!host) return null; // host dropped by a structural rebuild; render() will rebuild.
    host.classList.remove('cm-table-block-cell-static');
    host.replaceChildren(); // drop the static markdown snapshot
    // INVARIANT: a cell reveals raw markdown at the caret ONLY while focused; on blur it
    // renders fully formatted (like a static table cell). Why: an unfocused cell left with
    // the caret inside `**bold**` would otherwise keep showing the raw `**` markers. Driven
    // by a focus-toggled compartment that overrides the cell's base revealAtCursor.
    const revealComp = new Compartment();
    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: ytext.toString(),
        extensions: [
          editableCellExtensions(),
          revealComp.of(revealAtCursor.of(false)),
          EditorView.domEventHandlers({
            // WHY echo the selection: the live-preview StateFields rebuild on
            // docChanged/selection/syntax change but NOT on a bare reconfigure, so a
            // facet-only toggle would leave the decorations stale (formatting stuck
            // revealed). Re-setting the current range forces the rebuild that re-reads
            // revealAtCursor.
            focus: (_e, v) => { v.dispatch({ effects: revealComp.reconfigure(revealAtCursor.of(true)), selection: v.state.selection }); return false; },
            blur: (_e, v) => { v.dispatch({ effects: revealComp.reconfigure(revealAtCursor.of(false)), selection: v.state.selection }); return false; },
          }),
          createYjsExtension(ytext),
          this.cellNavKeymap(row, col),
          this.cellActiveViewListener(),
        ],
      }),
    });
    this.cellViews.set(key, view);
    // A newly-mounted editor lays out over the next frame — re-measure so CM6 reserves the
    // cell's real height (see the cellActiveViewListener INVARIANT on overflow/clicks).
    this.view?.requestMeasure();
    return view;
  }

  /**
   * Register the focused cell as the ACTIVE editor view so the document's shared UI —
   * the selection formatting toolbar (reads getEditorView() + UI-store selectionEmpty) —
   * targets this cell. On blur, restore the outer document view so the toolbar follows
   * focus back. Without this the toolbar never appears for a cell (its view is unknown).
   */
  private cellActiveViewListener() {
    const outer = this.view;
    return EditorView.updateListener.of((update) => {
      const cellView = update.view;
      if (update.focusChanged) {
        if (cellView.hasFocus) claimNestedView(cellView);
        else if (outer) claimNestedView(outer);
      }
      if ((update.selectionSet || update.focusChanged) && cellView.hasFocus) {
        useUIStore.getState().setSelectionEmpty(cellView.state.selection.main.empty);
      }
      // WHY: a cell's height can grow AFTER the block widget was first measured
      // (yCollab fills/wraps content asynchronously). CM6 reserves block-widget height from a
      // one-time DOM measure, so without forcing a re-measure the grown table overflows its  Why: CM6 measures block-widget height once; yCollab fills content async and a cell can grow later, so without a forced re-measure the table overflows its reserved height.
      // reserved box and rows below the first overlap — and steal clicks for the next document
      // line. Re-measure the OUTER view when a cell's GEOMETRY changes. Why only geometryChanged,
      // not docChanged: a content edit that does not change the cell's height needs no re-measure,
      // and firing requestMeasure on every keystroke re-runs the outer editor's full block-widget
      // measurement pass (O(doc)) — a per-keystroke cost that compounds across tables. CM6 sets
      // geometryChanged precisely when a cell's measured height actually changes.
      if (update.geometryChanged) outer?.requestMeasure();
    });
  }

  /**
   * Tab/Shift-Tab navigate between cells (NEVER create rows/columns — structure is
   * only ever changed via the toolbar). Enter/Shift-Enter insert an intra-cell line.
   * INVARIANT: Enter does not create a table row and Tab does not create a cell.
   * Why: rows/columns are added exclusively through the bottom-right toolbar buttons;
   * keystrokes stay inside the focused cell.
   */
  private cellNavKeymap(row: number, col: number) {
    return Prec.highest(keymap.of([
      { key: 'Tab', run: () => this.tabNext(row, col),
        shift: () => this.tabPrev(row, col) },
      { key: 'Enter', run: insertNewlineAndIndent },
      { key: 'Shift-Enter', run: insertNewlineAndIndent },
    ]));
  }

  /** Tab: next cell (wrapping row→row). Tab on the last (bottom-right) cell exits the table. */
  private tabNext(row: number, col: number): boolean {
    if (!this.doc) return true;
    const model = readTableModel(this.doc, this.tableId);
    if (!model) return true;
    const cols = model.columns.length;
    let c = col + 1;
    let r = row;
    if (c >= cols) { c = 0; r += 1; }
    if (r >= model.rows.length) { this.exitAfterTable(); return true; }
    this.focusCell(r, c);
    return true;
  }

  /** Shift-Tab: previous cell (wrapping row→row); no-op at the first cell. */
  private tabPrev(row: number, col: number): boolean {
    if (!this.doc) return true;
    const model = readTableModel(this.doc, this.tableId);
    if (!model) return true;
    const cols = model.columns.length;
    let c = col - 1;
    let r = row;
    if (c < 0) { c = cols - 1; r -= 1; }
    if (r < 0) return true;
    this.focusCell(r, c);
    return true;
  }

  /**
   * Blur the table and drop the host-doc caret just after the table anchor, past a
   * whitespace char (inserting a space if none follows the anchor). Mirrors the anchor
   * lookup in `removeAnchor`.
   */
  private exitAfterTable(): void {
    if (!this.view) return;
    const needle = tableAnchor(this.label, this.tableId);
    const text = this.view.state.doc.toString();
    const at = text.indexOf(needle);
    if (at < 0) return;
    let pos = at + needle.length;
    const next = text[pos];
    if (next === undefined || !/\s/.test(next)) {
      this.view.dispatch({ changes: { from: pos, insert: ' ' } });
    }
    pos += 1; // land past the whitespace char that follows the table
    this.view.focus();
    this.view.dispatch({ selection: { anchor: pos } });
  }

  private focusCell(row: number, col: number): void {
    const view = this.ensureCellEditor(row, col);
    if (!view) return;
    view.focus();
    view.dispatch({ selection: { anchor: view.state.doc.length } });
  }

  private teardownCells(): void {
    for (const view of this.cellViews.values()) view.destroy();
    this.cellViews.clear();
    this.cellHosts.clear();
  }

  /** Drag a header-cell border to set the column width (writes `w` to the model). */
  private startColResize(e: MouseEvent, col: number, th: HTMLElement): void {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = th.getBoundingClientRect().width;
    const onMove = (ev: MouseEvent) => {
      const w = Math.max(40, Math.round(startW + (ev.clientX - startX)));
      if (this.doc) setColumnWidth(this.doc, this.tableId, col, w);
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  }

  /**
   * Toolbar pinned to the table's bottom-right: insert row below / delete row / insert
   * column right / delete column — all relative to the focused cell (`activeCell`).
   * Icon-only buttons; SVGs mirror lucide `between-horizontal-end`/`between-vertical-end`
   * (inserts) with an `×` swapped for the chevron (deletes), sized to match the app's
   * `size={13}` toolbar convention.
   */
  private buildToolbar(): HTMLElement {
    const bar = document.createElement('div');
    bar.className = 'cm-table-block-toolbar';
    const mk = (svg: string, title: string, onClick: () => void, opts?: { dataAct?: string }) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = BTN_IDLE;
      b.title = title;
      b.setAttribute('aria-label', title);
      b.innerHTML = svg;
      if (opts?.dataAct) b.dataset.act = opts.dataAct;
      // mousedown + preventDefault: keep the focused cell (so `activeCell` stays valid).
      b.addEventListener('mousedown', (e) => { e.preventDefault(); onClick(); });
      return b;
    };
    bar.appendChild(mk(ICON_INSERT_ROW, t('tableInsertRowBelow'),
      () => { if (this.doc) addRow(this.doc, this.tableId, this.activeCell.row + 1); }));
    bar.appendChild(mk(ICON_DELETE_ROW, t('tableDeleteRow'),
      () => this.onDeleteButton('row'), { dataAct: ACT_DELETE_ROW }));
    bar.appendChild(mk(ICON_INSERT_COL, t('tableInsertColumnRight'),
      () => { if (this.doc) addColumn(this.doc, this.tableId, this.activeCell.col + 1); }));
    bar.appendChild(mk(ICON_DELETE_COL, t('tableDeleteColumn'),
      () => this.onDeleteButton('col'), { dataAct: ACT_DELETE_COL }));
    // disarm-on-mouseleave mirrors useArmedAction's React usage (e.g. RefCard onMouseLeave);
    // leaving an armed button cancels the pending confirmation.
    bar.querySelector(`[data-act="${ACT_DELETE_ROW}"]`)?.addEventListener('mouseleave', () => this.disarm('row'));
    bar.querySelector(`[data-act="${ACT_DELETE_COL}"]`)?.addEventListener('mouseleave', () => this.disarm('col'));
    // Re-apply the armed visual from instance state onto THIS bar (not this.toolbarEl,
    // which still points at the previous toolbar until render() reassigns it after we
    // return). A collaborative structural edit rebuilds this toolbar mid-arm, so without
    // this a red button would reset to idle.
    if (this.armedRow) {
      const b = bar.querySelector(`[data-act="${ACT_DELETE_ROW}"]`);
      if (b) b.className = BTN_ARMED;
    }
    if (this.armedCol) {
      const b = bar.querySelector(`[data-act="${ACT_DELETE_COL}"]`);
      if (b) b.className = BTN_ARMED;
    }
    return bar;
  }

  /**
   * Armed delete handler: first activation arms the button (turns it solid red), second
   * activation executes the delete and disarms. The minimum row/column guard runs BEFORE
   * arming so a no-op delete (last row/column) never shows a misleading armed state;
   * doDelete* keeps its own guard as a defensive second check.
   */
  private onDeleteButton(kind: 'row' | 'col'): void {
    if (kind === 'row') {
      if (!this.armedRow) {
        if (!this.canDelete('row')) return;
        this.arm('row');
        return;
      }
      this.disarm('row');
      this.doDeleteRow();
    } else {
      if (!this.armedCol) {
        if (!this.canDelete('col')) return;
        this.arm('col');
        return;
      }
      this.disarm('col');
      this.doDeleteColumn();
    }
  }

  /**
   * Minimum-size guard: false when the table has only one row/column of this kind. Single
   * source of truth for the floor — the arming path calls this BEFORE arming, and
   * `doDeleteRow`/`doDeleteColumn` delegate to it so the threshold cannot drift.
   * Uses `readTableShape` (array lengths only) instead of `readTableModel` (which
   * materializes every cell string as an O(cells) allocation per click).
   */
  private canDelete(kind: 'row' | 'col'): boolean {
    if (!this.doc) return false;
    const shape = readTableShape(this.doc, this.tableId);
    if (!shape) return false;
    return kind === 'row' ? shape.rows > 1 : shape.cols > 1;
  }

  /** Locate a delete button by `data-act` across the live toolbar DOM. */
  private deleteBtn(kind: 'row' | 'col'): HTMLElement | null {
    const act = kind === 'row' ? ACT_DELETE_ROW : ACT_DELETE_COL;
    return this.toolbarEl?.querySelector(`[data-act="${act}"]`) ?? null;
  }

  /** Swap a delete button's class between idle and armed. */
  private setBtnArmed(kind: 'row' | 'col', armed: boolean): void {
    const btn = this.deleteBtn(kind);
    if (btn) btn.className = armed ? BTN_ARMED : BTN_IDLE;
  }

  /**
   * Arm a delete button: mutual-disarm the other kind (two red buttons never coexist),
   * set the flag, start the auto-disarm timer, and apply the armed class.
   */
  private arm(kind: 'row' | 'col'): void {
    this.disarm(kind === 'row' ? 'col' : 'row');
    if (kind === 'row') {
      this.armedRow = true;
      this.rowArmTimer = setTimeout(() => this.disarm('row'), TableBlockWidget.ARM_TIMEOUT);
    } else {
      this.armedCol = true;
      this.colArmTimer = setTimeout(() => this.disarm('col'), TableBlockWidget.ARM_TIMEOUT);
    }
    this.setBtnArmed(kind, true);
  }

  /** Disarm a delete button: clear flag + timer and revert the armed class. */
  private disarm(kind: 'row' | 'col'): void {
    if (kind === 'row') {
      if (!this.armedRow) return;
      this.armedRow = false;
      if (this.rowArmTimer) { clearTimeout(this.rowArmTimer); this.rowArmTimer = undefined; }
    } else {
      if (!this.armedCol) return;
      this.armedCol = false;
      if (this.colArmTimer) { clearTimeout(this.colArmTimer); this.colArmTimer = undefined; }
    }
    this.setBtnArmed(kind, false);
  }

  private doDeleteRow(): void {
    if (!this.doc || !this.canDelete('row')) return;
    const model = readTableModel(this.doc, this.tableId);
    if (!model) return; // lost a race — next observe tick retries
    removeRow(this.doc, this.tableId, Math.min(this.activeCell.row, model.rows.length - 1));
  }

  private doDeleteColumn(): void {
    if (!this.doc || !this.canDelete('col')) return;
    const model = readTableModel(this.doc, this.tableId);
    if (!model) return; // lost a race — next observe tick retries
    removeColumn(this.doc, this.tableId, Math.min(this.activeCell.col, model.columns.length - 1));
  }

  /** Orphan anchor (no model / broken model) → explicit inline error + remove affordance. */
  private renderMissing(): HTMLElement {
    const box = document.createElement('div');
    box.className = 'cm-table-block-error';

    const msg = document.createElement('span');
    msg.textContent = `${t('tableBlockMissing')} — ${t('tableBlockMissingHint')}`;
    box.appendChild(msg);

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cm-table-block-remove';
    btn.textContent = t('tableBlockRemove');
    btn.addEventListener('mousedown', (e) => { e.preventDefault(); this.removeAnchor(); });
    box.appendChild(btn);
    return box;
  }

  /**
   * Read-only (snapshot preview) missing-model state: the checkpoint did not capture this
   * table's state (legacy row, or a preview-local doc with no matching model). Distinct
   * from `renderMissing` — no "remove anchor?" button (a preview can't edit), and a
   * message that names the real cause so it is visibly different from an empty table
   * (no-silent-degradation).
   */
  private renderMissingReadonly(): HTMLElement {
    const box = document.createElement('div');
    box.className = 'cm-table-block-error';
    const msg = document.createElement('span');
    msg.textContent = t('tableBlockSnapshotMissing');
    box.appendChild(msg);
    return box;
  }

  /** Delete the `![label](table:id)` anchor from the doc text + drop any stray model. */
  private removeAnchor(): void {
    if (this.view) {
      const needle = tableAnchor(this.label, this.tableId);
      const at = this.view.state.doc.toString().indexOf(needle);
      if (at >= 0) {
        this.view.dispatch({ changes: { from: at, to: at + needle.length, insert: '' } });
      }
    }
    if (this.doc) deleteTable(this.doc, this.tableId);
  }

  destroy(): void {
    this.teardownCells();
    // Clear armed timers so a pending auto-disarm never fires on a dead widget.
    if (this.rowArmTimer) { clearTimeout(this.rowArmTimer); this.rowArmTimer = undefined; }
    if (this.colArmTimer) { clearTimeout(this.colArmTimer); this.colArmTimer = undefined; }
    this.unobserve?.();
    this.unobserve = null;
    this.unsubEntity?.();
    this.unsubEntity = null;
    // INVARIANT(data-loss) (CM6 eviction/reuse): destroy() MUST drop the cached DOM —
    // CM6 drops a block widget's DOM on viewport eviction and REUSES the same instance
    // on scroll-back (eq matches), calling toDOM again on a fresh wrap. With tableEl
    // still set, render() takes the same-shape widths-only fast path against the STALE
    // detached node and returns without appending anything — the anchor line renders
    // blank, i.e. the table vanishes. Why: the cache exists to keep cell editors alive
    // across keystrokes (see the structural-rebuild INVARIANT above); it is only valid
    // while THIS DOM is mounted.
    // The SHAPE counters (renderedRowCount/lastColCount) are deliberately NOT reset:
    // nulling tableEl alone already forces the structural-rebuild path, while
    // estimatedHeight reads renderedRowCount precisely for widgets whose DOM is gone —
    // zeroing it would collapse an evicted table's height estimate to the padding and
    // make the scrollbar jump on scroll-back.
    this.tableEl = null;
    this.toolbarEl = null;
  }

  get estimatedHeight(): number {
    // WHY cached, not readTableModel: CM6 invokes this getter during every measurement pass
    // (and requestMeasure re-runs it). Materializing the full model (all cell strings) here
    // would allocate O(cells) per pass. The row count is cached at render time and is all the
    // estimate needs; before first render it falls back to 2 rows.
    return this.renderedRowCount * ESTIMATED_ROW_HEIGHT + WRAP_PADDING;
  }
}
