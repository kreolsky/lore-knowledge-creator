/**
 * Find-match highlighter — decorates EVERY query match, keyed on the search query
 * (not the selection). A CM6 decoration layer for the Find tab (see SYSTEM: editor).
 *
 * WHY a custom plugin: the stock `@codemirror/search` `searchHighlighter` refuses
 * to decorate unless its OWN bottom panel is mounted
 * (`node_modules/@codemirror/search/dist/index.js`: `if (!panel || !query.spec.valid)
 * return Decoration.none`). CodeMirrorEditor deliberately drops `searchKeymap`/
 * `openSearchPanel` and renders a React panel (FindReplacePanel) instead, so `panel`
 * is permanently null and `.cm-searchMatch` never renders. The intermittent
 * highlight users saw was `highlightSelectionMatches()` (`.cm-selectionMatch`) firing
 * after findNext moved the selection — keyed on the selected TEXT, not the query, so
 * it disagreed with case/regexp/whole-word options and stayed dark while typing.
 *
 * The match set lives in `searchMatchesField`, which walks the SAME cursor the
 * Next/Prev/Replace-all commands act on (`SearchQuery.getCursor`) — so the
 * highlighted set is by construction identical to what those commands target,
 * including case/whole-word/regexp semantics. The field re-derives only on query/doc
 * change (never on a bare cursor move), and both the decoration plugin and the
 * Find-panel count read it instead of re-walking. A `.cm-searchMatch-selected` class
 * marks the match under the primary selection (the one findNext just landed on).
 */
import { Text, type EditorState, type Extension, Compartment, RangeSetBuilder, StateEffect, StateField } from '@codemirror/state';
import {
  ViewPlugin, type ViewUpdate, EditorView, Decoration, type DecorationSet,
} from '@codemirror/view';
import { getSearchQuery, type SearchQuery } from '@codemirror/search';
import type * as Y from 'yjs';
import {
  cellKey,
  getCellText as readCellText,
  getTablesMap,
  orderedColIds,
  orderedRowIds,
  type ColId,
  type RowId,
} from './live-preview/table-block-model';
import { TABLE_ANCHOR_RE } from './live-preview/table-document-ops';
import { tableDocSource } from './live-preview/table-doc-source';
import { entityYdoc, getViewEntity } from '../../editor/active-editor';
import { subscribeEntityHandle } from '../../collab/active-handle-registry';

/** CM6's own cap (matchAll uses 1000). Over it we report `1000+`, never silently
 *  truncating — a partially-highlighted doc that looks complete is the failure mode
 *  this feature removes. */
export const SEARCH_HIGHLIGHT_LIMIT = 1000;

/** True when two queries find the SAME matches — `SearchQuery.eq` also compares the
 *  replacement text, so typing in the Replace field would otherwise re-walk the host
 *  and every cell and reset the panel's navigation over an unchanged match set. */
export function sameSearch(a: SearchQuery, b: SearchQuery): boolean {
  return a.search === b.search && a.caseSensitive === b.caseSensitive && a.regexp === b.regexp
    && a.wholeWord === b.wholeWord && a.literal === b.literal;
}

export interface MatchRange {
  from: number;
  to: number;
}

export interface CollectResult {
  ranges: MatchRange[];
  truncated: boolean;
}

/**
 * Pure: collect every match of `query` in `state.doc`, capped at `limit`.
 *
 * Uses `SearchQuery.getCursor` (the public wrapper over the internal Query used by
 * findNext/replaceAll), so the result set is identical to what the Find commands act
 * on — no separate case/whole-word/regexp logic to drift. Zero-length matches (e.g.
 * `a*`) are skipped: the cursor's `.next()` advances past them (no infinite loop),
 * and an empty `{from,to}` decoration is invisible anyway.
 */
export function collectMatches(
  state: EditorState,
  query: SearchQuery,
  limit: number = SEARCH_HIGHLIGHT_LIMIT,
): CollectResult {
  // query.valid is false for an empty search string OR a syntactically-invalid regexp.
  if (!query.valid) return { ranges: [], truncated: false };
  const ranges: MatchRange[] = [];
  let truncated = false;
  const cursor = query.getCursor(state);
  for (;;) {
    const step = cursor.next();
    if (step.done) break;
    const { from, to } = step.value;
    if (from === to) continue; // zero-length (e.g. `a*`) — no visible mark
    if (ranges.length >= limit) { truncated = true; break; }
    ranges.push({ from, to });
  }
  return { ranges, truncated };
}

const matchMark = Decoration.mark({ class: 'cm-searchMatch' });
const selectedMark = Decoration.mark({ class: 'cm-searchMatch cm-searchMatch-selected' });

/**
 * Single source of truth for the match set: derived from the query + doc ONLY (not the
 * selection), so a bare cursor move never triggers a full-document `getCursor` walk.
 * Both the decoration plugin and the Find-panel count read this field instead of
 * re-walking — one scan per query/doc change, shared by both consumers. */
export const searchMatchesField = StateField.define<CollectResult>({
  create(state) {
    return collectMatches(state, getSearchQuery(state));
  },
  update(value, tr) {
    if (tr.docChanged || !sameSearch(getSearchQuery(tr.startState), getSearchQuery(tr.state))) {
      return collectMatches(tr.state, getSearchQuery(tr.state));
    }
    return value;
  },
});

function buildDecorations(view: EditorView): DecorationSet {
  // Ranges come from the cached field — no getCursor walk here.
  const { ranges } = view.state.field(searchMatchesField, false) ?? { ranges: [], truncated: false };
  const sel = view.state.selection;
  // matches come ascending + non-overlapping from getCursor → RangeSetBuilder is valid.
  const builder = new RangeSetBuilder<Decoration>();
  for (const m of ranges) {
    // Mirrors the stock highlighter: the match is "selected" when a selection range
    // equals it exactly (findNext/prev set the selection to the match range).
    const selected = sel.ranges.some(r => r.from === m.from && r.to === m.to);
    builder.add(m.from, m.to, selected ? selectedMark : matchMark);
  }
  return builder.finish();
}

/**
 * Selection-aware decoration plugin. Reads the cached match ranges from
 * searchMatchesField and rebuilds only the (cheap) decoration RangeSet + the
 * `-selected` class on query/doc/selection change — the expensive `getCursor` walk
 * lives in the field, run once per query/doc change (never on a bare cursor move).
 * Unlike the stock highlighter it covers the WHOLE doc (capped), not `visibleRanges`,
 * so off-screen matches are highlighted too. */
export const searchHighlightPlugin = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = buildDecorations(view); }
  update(u: ViewUpdate) {
    if (u.docChanged || u.selectionSet || !sameSearch(getSearchQuery(u.startState), getSearchQuery(u.state))) {
      this.decorations = buildDecorations(u.view);
    }
  }
}, { decorations: v => v.decorations });

/** Bundle: the match-set field + the decoration plugin. Include this wherever the
 *  search() StateField lives. */
export const searchHighlight: Extension = [searchMatchesField, searchHighlightPlugin];

/**
 * Compartment FindReplacePanel reconfigures with an `updateListener` on mount (and
 * clears on unmount), so the match-count / current-index readout stays live as the
 * query, doc, or selection changes. Kept inert ([]) in every editor that does not
 * host a Find panel (mini editors never reconfigure it). Why a Compartment: it is the
 * only leak-free way to attach then REMOVE an updateListener from a view the panel
 * does not own (appendConfig cannot be surgically reversed).
 */
export const searchPanelListenerCompartment = new Compartment();

// ─── Combined match list: host text + every anchored table's cells ──────────
//
// ARCH: cell text lives in per-cell Y.Texts OUTSIDE the host buffer, so the
// field above can never see it. The combined index runs the SAME SearchQuery
// over each cell (getCursor over the cell string — case/whole-word/regexp
// semantics identical by construction, `^`/`$` anchoring per cell LINE) and
// merges host + cell matches into ONE document-ordered list: host position,
// a cell match sorting at its table anchor's host position, then the model's
// row/column order, from. A cell is identified by (tableId, rowId, colId); its
// `ord` (row-major index in the table's current order) only orders matches. A host match that intersects a table anchor is dropped (the anchor
// range is replaced by the table widget — no text to land in). Anchored tables
// are enumerated from the host's anchors (first occurrence of an id), NEVER
// from the tables map (which also holds unanchored models). No GFM offsets
// anywhere: cell offsets are UTF-16 units in the cell's own Y.Text — the same
// coordinate space a mounted cell editor and Yjs deltas speak.

/** One `![label](table:id)` anchor occurrence in the host text. */
export interface AnchorSpan {
  id: string;
  from: number;
  to: number;
}

/** A Find match over the WHOLE document: host text or one table cell. */
export type FindMatch =
  | { kind: 'host'; from: number; to: number }
  | { kind: 'cell'; tableId: string; rowId: RowId; colId: ColId; ord: number; from: number; to: number };

/** The combined list: every match in document order, capped at SEARCH_HIGHLIGHT_LIMIT. */
export interface CombinedMatchResult {
  matches: FindMatch[];
  truncated: boolean;
}

/** Anchored tables of a host text in document order — FIRST occurrence of an id
 *  only (a duplicated anchor is the reconcile layer's to clone, not ours to
 *  double-count). Unanchored tables are absent by construction. */
export function collectAnchorSpans(hostText: string): AnchorSpan[] {
  const out: AnchorSpan[] = [];
  const seen = new Set<string>();
  for (const m of hostText.matchAll(TABLE_ANCHOR_RE)) {
    const id = m[2];
    if (seen.has(id)) continue;
    seen.add(id);
    out.push({ id, from: m.index, to: m.index + m[0].length });
  }
  return out;
}

/** Drop host matches that intersect ANY anchor span (partly or wholly). */
export function dropAnchorCrossing(ranges: MatchRange[], anchors: AnchorSpan[]): MatchRange[] {
  if (anchors.length === 0) return ranges;
  return ranges.filter(r => !anchors.some(a => r.from < a.to && a.from < r.to));
}

/** Scan ONE cell's text with the same SearchQuery the stock commands use. */
export function scanCellMatches(
  query: SearchQuery,
  cellText: string,
  limit: number = SEARCH_HIGHLIGHT_LIMIT,
): MatchRange[] {
  if (!query.valid) return [];
  const ranges: MatchRange[] = [];
  // A cell is scanned as its own doc (Text.of re-joins lines with \n) — ^/$ anchor
  // per cell line, offsets UTF-16 in the cell text.
  const cursor = query.getCursor(Text.of(cellText.split('\n')));
  for (;;) {
    const step = cursor.next();
    if (step.done) break;
    const { from, to } = step.value;
    if (from === to) continue; // zero-length (e.g. `a*`) — no visible mark
    if (ranges.length >= limit) break;
    ranges.push({ from, to });
  }
  return ranges;
}

/** Inputs the combined index reads at derive time. Injected so the pure core is
 *  testable without a live view/ydoc (see find-matches.test.ts). */
export interface CombinedIndexInputs {
  getQuery(): SearchQuery;
  /** Host match set as the field holds it (may cross anchors; may be truncated). */
  getHostResult(): CollectResult;
  getHostText(): string;
  /** Live cell text, or null when the cell/table/model is absent. */
  getCellText(tableId: string, rowId: RowId, colId: ColId): string | null;
  /** Row and column ids in the model's display order, or null when the table is absent. */
  getTableAxes(tableId: string): { rowIds: readonly RowId[]; colIds: readonly ColId[] } | null;
}

/**
 * Pure combined-match index for one host view + its tables doc. Owns the
 * per-cell match cache — keyed (tableId, cellKey(rowId, colId)) for the CURRENT query — and
 * the merged document-ordered list. Invalidation rules:
 *   - query change             → whole cell cache cleared (checked at derive time)
 *   - host doc change          → host/anchors re-read, cell cache KEPT (a host
 *                                edit moves anchors but never changes a cell)
 *   - one cell changed         → cellChanged(tableId, cellKey)
 *   - row/col added, removed or moved, table replaced → tableChanged(tableId)
 *   - whole tables subtree / doc rebind        → allTablesChanged()
 *
 * `cellScanCount` counts real per-cell scans (cache misses) — the observable
 * the cache exists for.
 */
export class CombinedMatchIndex {
  private query: SearchQuery | null = null;
  private cellCache = new Map<string, Map<string, MatchRange[]>>();
  private result: CombinedMatchResult = { matches: [], truncated: false };
  private dirty = true;
  cellScanCount = 0;

  constructor(private readonly inputs: CombinedIndexInputs) {}

  /** Host doc and/or query changed: re-read host inputs at the next derive. */
  hostChanged(): void { this.dirty = true; }

  cellChanged(tableId: string, key: string): void {
    this.cellCache.get(tableId)?.delete(key);
    this.dirty = true;
  }

  tableChanged(tableId: string): void {
    this.cellCache.delete(tableId);
    this.dirty = true;
  }

  allTablesChanged(): void {
    this.cellCache.clear();
    this.dirty = true;
  }

  private cellRanges(query: SearchQuery, tableId: string, rowId: RowId, colId: ColId): MatchRange[] {
    let table = this.cellCache.get(tableId);
    if (!table) {
      table = new Map();
      this.cellCache.set(tableId, table);
    }
    const key = cellKey(rowId, colId);
    const cached = table.get(key);
    if (cached) return cached;
    const text = this.inputs.getCellText(tableId, rowId, colId);
    if (text === null) return []; // absent cell — nothing to cache (shape growth is a tableChanged)
    this.cellScanCount++;
    const ranges = scanCellMatches(query, text);
    table.set(key, ranges);
    return ranges;
  }

  derive(): CombinedMatchResult {
    const query = this.inputs.getQuery();
    if (!this.query || !sameSearch(this.query, query)) {
      this.cellCache.clear();
      this.dirty = true;
    }
    this.query = query;
    if (!query.valid) {
      this.dirty = false;
      this.result = { matches: [], truncated: false };
      return this.result;
    }
    if (!this.dirty) return this.result;
    this.dirty = false;

    const hostResult = this.inputs.getHostResult();
    const anchors = collectAnchorSpans(this.inputs.getHostText());
    const host = dropAnchorCrossing(hostResult.ranges, anchors);
    const matches: FindMatch[] = [];
    const truncated = hostResult.truncated;
    let capped = false;
    const push = (m: FindMatch): boolean => {
      if (matches.length >= SEARCH_HIGHLIGHT_LIMIT) {
        capped = true;
        return false;
      }
      matches.push(m);
      return true;
    };

    let hi = 0;
    for (const a of anchors) {
      // Host matches ending at/before the anchor sort ahead of its cells.
      while (hi < host.length && host[hi].to <= a.from) {
        if (!push({ kind: 'host', from: host[hi].from, to: host[hi].to })) break;
        hi++;
      }
      if (capped) break;
      const axes = this.inputs.getTableAxes(a.id);
      if (!axes) continue; // orphan anchor — no model to scan
      let ord = 0;
      for (const rowId of axes.rowIds) {
        for (const colId of axes.colIds) {
          for (const r of this.cellRanges(query, a.id, rowId, colId)) {
            if (!push({ kind: 'cell', tableId: a.id, rowId, colId, ord, from: r.from, to: r.to })) break;
          }
          ord++;
          if (capped) break;
        }
        if (capped) break;
      }
      if (capped) break;
    }
    while (hi < host.length && !capped) {
      if (!push({ kind: 'host', from: host[hi].from, to: host[hi].to })) break;
      hi++;
    }

    this.result = { matches, truncated: truncated || capped };
    return this.result;
  }
}

// ─── Cell marks: what the Find panel paints inside table cells ──────────────
//
// ARCH: one mark source for host text, static placeholders and mounted cells —
// the combined index above. The host's own matches stay in searchMatchesField;
// cell marks travel two StateEffect hops, each owned by its publisher:
//  - setCellMarksByTable (HOST view): the index publishes the grouped per-table
//    sets into cellMarksByTableField; cellMarksRelay (table-relays, the
//    widgetsByWrap owner) hands each mounted widget its table's entry, and a
//    widget mounting later — scrolled into view, or a structural rebuild —
//    pulls the same field at build time.
//  - setCellMatches (CELL view): one cell's ranges as `cm-searchMatch` marks in
//    cellMatchesField — mounted in editorExperience mode 'cell', so a mounted
//    cell editor and the shared static placeholder renderer (the `marks`
//    option in cell-static-render) paint the SAME classes as the host.

/** tableId → cellKey(rowId, colId) → match offsets in that cell's own Y.Text. */
export type CellMarksByTable = ReadonlyMap<string, ReadonlyMap<string, readonly MatchRange[]>>;

/** Publish the combined index's current per-cell marks into the HOST view. */
export const setCellMarksByTable = StateEffect.define<CellMarksByTable>();

export const cellMarksByTableField = StateField.define<CellMarksByTable>({
  create: () => new Map(),
  update: (value, tr) => {
    for (const e of tr.effects) if (e.is(setCellMarksByTable)) return e.value;
    return value;
  },
});

/** Group a combined list's CELL matches per table, keyed the way the table
 *  widget keys its cells (`cellKey(rowId, colId)`); host matches are absent by construction.
 *  A table whose every match disappeared simply has no entry — the relay hands
 *  the widget an empty map, which is what clears that table's marks. */
export function groupCellMarks(matches: readonly FindMatch[]): Map<string, Map<string, MatchRange[]>> {
  const byTable = new Map<string, Map<string, MatchRange[]>>();
  for (const m of matches) {
    if (m.kind !== 'cell') continue;
    let cells = byTable.get(m.tableId);
    if (!cells) byTable.set(m.tableId, cells = new Map());
    const key = cellKey(m.rowId, m.colId);
    const ranges = cells.get(key);
    if (ranges) ranges.push({ from: m.from, to: m.to });
    else cells.set(key, [{ from: m.from, to: m.to }]);
  }
  return byTable;
}

/** One cell's find-match ranges (offsets in the cell's own text); empty clears. */
export const setCellMatches = StateEffect.define<readonly MatchRange[]>();

/** The cell match the panel's Next/Prev last landed on — the CURRENT match when
 *  it lives inside a table (null when it is a host match, or none). Lives on the
 *  HOST view: the relay in table-block-widget fans it out to the widgets, which
 *  paint it as a mounted cell's `cm-searchMatch-selected` range or as the
 *  static cell's current class. Offsets are the cell's own — a HOST doc change
 *  never remaps them (the panel re-points the field per navigation). */
export interface CurrentCellMatch {
  tableId: string;
  rowId: RowId;
  colId: ColId;
  from: number;
  to: number;
}

/** Host-side slot for the current cell match (see CurrentCellMatch). */
export const setCurrentCellMatch = StateEffect.define<CurrentCellMatch | null>();

export const currentCellMatchField = StateField.define<CurrentCellMatch | null>({
  create: () => null,
  update: (value, tr) => {
    for (const e of tr.effects) if (e.is(setCurrentCellMatch)) return e.value;
    // WHY: the user moved the host caret or typed in the host — Next/Prev must now
    // start from where they are, not from the cell they left. A peer's remote edit
    // carries no such userEvent and keeps the current cell match.
    if (value && (tr.isUserEvent('select') || tr.isUserEvent('input') || tr.isUserEvent('delete'))) return null;
    return value;
  },
});

/** The cell-side mark sink. Fed only by `setCellMatches`; maps through local
 *  edits so a mark tracks its text between the edit and the next re-derive. */
export const cellMatchesField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, tr) {
    let next = value.map(tr.changes);
    for (const e of tr.effects) if (e.is(setCellMatches)) {
      // Pushed ranges are freshly derived — rebuild, don't map them.
      next = Decoration.set(e.value.map(r => matchMark.range(r.from, r.to)), true);
    }
    return next;
  },
  provide: f => EditorView.decorations.from(f),
});

/** The CURRENT match inside one mounted cell (offsets in the cell's own text);
 *  null clears. The widget pushes it as the relay's fan-out of the host-side
 *  setCurrentCellMatch — the cell-side twin of the host's `-selected` class. */
export const setCellCurrentMatch = StateEffect.define<MatchRange | null>();

export const cellCurrentMatchField = StateField.define<MatchRange | null>({
  create: () => null,
  update(value, tr) {
    let next = value;
    if (value && tr.docChanged) {
      const from = tr.changes.mapPos(value.from);
      const to = tr.changes.mapPos(value.to);
      // The edit consumed the range — no zero-length mark, just clear.
      next = from < to ? { from, to } : null;
    }
    for (const e of tr.effects) if (e.is(setCellCurrentMatch)) {
      next = e.value && e.value.from < e.value.to ? e.value : null;
    }
    return next;
  },
  provide: f => EditorView.decorations.from(f, cur => (
    cur ? Decoration.set([selectedMark.range(cur.from, cur.to)]) : Decoration.none
  )),
});

// ─── View-bound half: feeds the index from one host view ───────────────────

export interface FindIndexHandle {
  /** The combined list, derived synchronously (cache-backed; cheap when clean). */
  matches(): CombinedMatchResult;
  destroy(): void;
}

const raf: (fn: () => void) => number =
  typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => window.setTimeout(fn, 0);
const cancelRaf: (handle: number) => void =
  typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : (handle) => window.clearTimeout(handle);

/**
 * Bind a CombinedMatchIndex to one host view and notify `changed` (debounced to
 * one frame) whenever the combined set may have moved: host transactions (via
 * the listener compartment) and peer edits to the tables subtree (a deep
 * observer on the tables map — a peer editing a cell produces NO host
 * transaction). The tables doc is `tableDocSource` first (the snapshot preview
 * reads its preview-local doc, never the live handle), else the view's entity
 * handle, late-bound through the handle registry for the join race.
 */
export function attachFindIndex(view: EditorView, changed: () => void): FindIndexHandle {
  let ydoc: Y.Doc | null = view.state.facet(tableDocSource) ?? entityYdoc(view) ?? null;
  let unobserveTables: (() => void) | null = null;
  let unsubEntity: (() => void) | null = null;
  let destroyed = false;
  let frame: number | null = null;

  const index = new CombinedMatchIndex({
    getQuery: () => getSearchQuery(view.state),
    getHostResult: () => view.state.field(searchMatchesField, false) ?? { ranges: [], truncated: false },
    getHostText: () => view.state.doc.toString(),
    getCellText: (id, rowId, colId) => (ydoc ? readCellText(ydoc, id, rowId, colId)?.toString() ?? null : null),
    getTableAxes: (id) => {
      const rowIds = ydoc ? orderedRowIds(ydoc, id) : null;
      const colIds = ydoc ? orderedColIds(ydoc, id) : null;
      return rowIds && colIds ? { rowIds, colIds } : null;
    },
  });

  const schedule = (): void => {
    if (destroyed || frame != null) return;
    frame = raf(() => {
      frame = null;
      if (!destroyed) changed();
    });
  };

  const observeTables = (doc: Y.Doc): void => {
    unobserveTables?.();
    const tables = getTablesMap(doc);
    // Same detach pattern as the table widget: observeDeep's callback is stored
    // and removed via unobserveDeep (yjs's observeDeep itself returns void).
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- yjs's own callback shape
    const cb = (events: Y.YEvent<any>[]) => {
      for (const ev of events) {
        const p = ev.path;
        if (typeof p[0] !== 'string') {
          // Event on the tables map itself: a table set/deleted wholesale.
          index.allTablesChanged();
          continue;
        }
        const [tableId, part, key] = p;
        if (part === 'cells' && typeof key === 'string') {
          // Text edit inside one cell's Y.Text.
          index.cellChanged(tableId, key);
        } else if (part === 'cells') {
          // Cells created/deleted (eager add/remove, first write of a race cell).
          for (const k of ev.changes.keys.keys()) index.cellChanged(tableId, k);
        } else if ((part === 'rows' || part === 'columns') && key !== undefined) {
          // WHY a non-`pos` axis write is skipped: a column entry's `w` is written on
          // every pointer move of a width drag, and no cell text or order changes.
          if (ev.changes.keys.has('pos')) index.tableChanged(tableId);
        } else {
          // Axis key set changed (row/column added or removed) or the table entry itself.
          index.tableChanged(tableId);
        }
      }
      schedule();
    };
    tables.observeDeep(cb);
    unobserveTables = () => tables.unobserveDeep(cb);
  };

  const bindDoc = (doc: Y.Doc | null): void => {
    if (doc === ydoc) return;
    ydoc = doc;
    index.allTablesChanged();
    if (doc) observeTables(doc);
    else {
      unobserveTables?.();
      unobserveTables = null;
    }
    schedule();
  };

  if (ydoc) observeTables(ydoc);
  else {
    // Late-bind the entity's handle (collab join may still be in flight), and
    // re-bind on handle replacement — the same contract the table widget holds.
    const entityId = getViewEntity(view);
    if (entityId) unsubEntity = subscribeEntityHandle(entityId, (handle) => bindDoc(handle?.ydoc ?? null));
  }

  const listener = EditorView.updateListener.of((u) => {
    const queryChanged = !sameSearch(getSearchQuery(u.state), getSearchQuery(u.startState));
    if (u.docChanged || queryChanged) index.hostChanged();
    if (u.docChanged || u.selectionSet || queryChanged) schedule();
  });
  view.dispatch({ effects: searchPanelListenerCompartment.reconfigure(listener) });
  index.hostChanged();

  // Publish the derived cell marks into the host view whenever a re-derive
  // produced a NEW result object (a clean derive returns the cached one by
  // identity — selection-only recomputes republish nothing). The relay in
  // table-block-widget fans the entry out to the table widgets.
  let published: CombinedMatchResult | null = null;
  const publish = (result: CombinedMatchResult): void => {
    if (result === published) return;
    published = result;
    view.dispatch({ effects: setCellMarksByTable.of(groupCellMarks(result.matches)) });
  };

  return {
    matches: () => {
      const result = index.derive();
      publish(result);
      return result;
    },
    destroy: () => {
      if (destroyed) return;
      destroyed = true;
      if (frame != null) cancelRaf(frame);
      frame = null;
      unobserveTables?.();
      unsubEntity?.();
      // The listener drops AND the marks clear in one dispatch: the panel is
      // going away, so the marks it painted must not linger in the cells.
      view.dispatch({
        effects: [
          searchPanelListenerCompartment.reconfigure([]),
          setCellMarksByTable.of(groupCellMarks([])),
        ],
      });
    },
  };
}
