/**
 * Table cell placeholders (see SYSTEM: table-block in `table-block-widget.ts`): what an
 * UNMOUNTED cell shows — its note marks, its find marks, the deferred markdown upgrade —
 * and the repaint of those placeholders from their live Y.Text.
 */
import type { EditorView } from '@codemirror/view';
import type * as Y from 'yjs';
import { getViewEntity } from '../../../editor/active-editor';
import { noteRangesByText } from '../note-anchor-wiring';
import type { NoteAnchorRange } from '../../../editor/note-highlight';
import { renderCellStatic, isPlainCellText } from './cell-static-render';
import {
  cellMarksByTableField,
  setCellCurrentMatch,
  setCellMatches,
  type CurrentCellMatch,
  type MatchRange,
} from '../find-matches';
import { cellKey, type RowId, type ColId } from './table-block-model';
import { NO_CELL_MARKS, sameRanges } from './table-relays';

/** Time slice for one deferred-render pass; the main thread is yielded between slices. */
const DEFERRED_RENDER_SLICE_MS = 12;

/** One cell's address and DOM: the `th`/`td`, the editor host inside it. */
export interface CellSlot {
  rowId: RowId;
  colId: ColId;
  el: HTMLElement;
  host: HTMLElement;
}

/** The widget state the painter reads, each read at call time (the maps are live). */
export interface PlaceholderHost {
  tableId: string;
  view: () => EditorView | null;
  doc: () => Y.Doc | null;
  readOnly: () => boolean;
  cells: ReadonlyMap<string, CellSlot>;
  cellViews: ReadonlyMap<string, EditorView>;
  slotText: (slot: CellSlot) => Y.Text | null;
}

export type PlaceholderPainter = ReturnType<typeof createPlaceholderPainter>;

export function createPlaceholderPainter(host: PlaceholderHost) {
  const { cells, cellViews } = host;
  /** This entity's resolved note ranges by Y.Text; null = recompute on next read. */
  let noteMap: Map<unknown, NoteAnchorRange[]> | null = null;
  /** Find-match ranges per cell as LAST painted (`cellKey` → ranges) — the
   *  change detector applyCellMarks re-renders only cells whose set moved. */
  let lastCellMarks = new Map<string, readonly MatchRange[]>();
  /** The panel's CURRENT cell match as last applied (relay / revealCellMatchInView).
   *  Kept across a structural patch — render() re-applies it onto fresh hosts. */
  let currentCellMatch: CurrentCellMatch | null = null;
  /** Markdown cells still showing raw text, by `cellKey`, in table order. */
  let deferredRenders: string[] = [];
  let deferredCursor = 0;
  let deferredTimer: ReturnType<typeof setTimeout> | null = null;

  /** Resolved note ranges inside `ytext`; none on a read-only host (a preview paints no notes). */
  function cellNotes(ytext: Y.Text | null | undefined): NoteAnchorRange[] | undefined {
    const view = host.view();
    if (host.readOnly() || !ytext || !view) return undefined;
    const entityId = getViewEntity(view);
    if (!entityId) return undefined;
    noteMap ??= noteRangesByText(entityId);
    return noteMap.get(ytext);
  }

  /** This table's host-published find-match marks (`cellKey` → ranges), straight
   *  from the host view's field — the single source a fresh build pulls and the
   *  relay's pushes agree with. In the snapshot preview the host view IS the preview
   *  view, so a preview builds its own preview-local marks. */
  function tableCellMarks(): ReadonlyMap<string, readonly MatchRange[]> {
    return host.view()?.state.field(cellMarksByTableField, false)?.get(host.tableId) ?? NO_CELL_MARKS;
  }

  function cellMarksFor(key: string): readonly MatchRange[] | undefined {
    return tableCellMarks().get(key);
  }

  /** Re-render one unmounted cell's placeholder from its CURRENT Y.Text. */
  function repaintPlaceholder(slot: CellSlot, marks: readonly MatchRange[] | undefined): void {
    const ytext = host.slotText(slot);
    slot.host.replaceChildren(renderCellStatic(ytext?.toString() ?? '', {
      notes: cellNotes(ytext),
      marks,
    }));
  }

  /**
   * The find index published this table's per-cell match marks: re-render static
   * placeholders whose set changed from their CURRENT Y.Text (an unmounted
   * placeholder observes nothing — see ensureCellEditor's INVARIANT), and feed
   * mounted cell editors via setCellMatches. A cell whose set did not move is
   * not touched — re-rendering is the O(cells) cost lazy mounting avoids.
   */
  function applyCellMarks(marks: ReadonlyMap<string, readonly MatchRange[]>): void {
    if (!host.doc()) return;
    let domChanged = false;
    const keys = new Set<string>([...lastCellMarks.keys(), ...marks.keys()]);
    for (const key of keys) {
      const next = marks.get(key);
      if (sameRanges(lastCellMarks.get(key), next)) continue;
      const view = cellViews.get(key);
      if (view) {
        view.dispatch({ effects: setCellMatches.of(next ?? []) });
        continue;
      }
      const slot = cells.get(key);
      if (!slot) continue; // not rendered (dropped by a patch); a rebuilt cell pulls fresh marks
      repaintPlaceholder(slot, next);
      domChanged = true;
    }
    lastCellMarks = new Map(marks);
    if (domChanged) host.view()?.requestMeasure();
  }

  /**
   * The panel moved its current-match cursor into (or out of) this table: a
   * MOUNTED cell receives the `cm-searchMatch-selected` range inside its editor
   * (setCellCurrentMatch); a STATIC cell — viewer/commentator, snapshot preview,
   * or an editable host's lazy placeholder — gets the cell-level current class
   * (its per-range marks already ride applyCellMarks). Idempotent: re-applying
   * the same position touches nothing.
   */
  function applyCurrentCellMatch(pos: CurrentCellMatch | null): void {
    const prev = currentCellMatch;
    currentCellMatch = pos;
    if (prev && (!pos || prev.rowId !== pos.rowId || prev.colId !== pos.colId)) {
      const prevKey = cellKey(prev.rowId, prev.colId);
      cellViews.get(prevKey)?.dispatch({ effects: setCellCurrentMatch.of(null) });
      cells.get(prevKey)?.host.classList.remove('cm-table-block-cell-current');
    }
    if (!pos) return;
    const key = cellKey(pos.rowId, pos.colId);
    const view = cellViews.get(key);
    const slotHost = cells.get(key)?.host;
    if (view) {
      // Mounted: the editor paints its own selected mark — drop the cell-level
      // class a placeholder era may have left on the shared host element.
      view.dispatch({ effects: setCellCurrentMatch.of({ from: pos.from, to: pos.to }) });
      slotHost?.classList.remove('cm-table-block-cell-current');
    } else {
      slotHost?.classList.add('cm-table-block-cell-current');
    }
  }

  /** The note list changed: re-render every placeholder that gains or loses a note mark. */
  function refreshNotedPlaceholders(): void {
    noteMap = null;
    if (!host.doc() || host.readOnly()) return;
    let changed = false;
    for (const [key, slot] of cells) {
      if (cellViews.has(key)) continue;
      const notes = cellNotes(host.slotText(slot));
      if (!notes?.length && !slot.host.querySelector('.cm-note-anchor')) continue;
      repaintPlaceholder(slot, cellMarksFor(key));
      changed = true;
    }
    if (changed) host.view()?.requestMeasure();
  }

  function scheduleDeferredRenders(): void {
    if (deferredTimer !== null || deferredCursor >= deferredRenders.length) return;
    deferredTimer = setTimeout(() => {
      deferredTimer = null;
      runDeferredRenders();
    }, 0);
  }

  /**
   * Upgrade raw-text markdown placeholders for one time slice, then yield. A cell whose live
   * editor mounted meanwhile is skipped (the editor owns it now); the text is re-read from the
   * Y.Text so a peer edit since the build is not reverted to a stale snapshot.
   */
  function runDeferredRenders(): void {
    if (!host.doc()) return;
    const until = performance.now() + DEFERRED_RENDER_SLICE_MS;
    while (deferredCursor < deferredRenders.length && performance.now() < until) {
      const key = deferredRenders[deferredCursor++];
      const slot = cells.get(key);
      if (!slot || cellViews.has(key)) continue;
      repaintPlaceholder(slot, cellMarksFor(key));
    }
    // Collapsed links change row heights — let CM6 re-measure the widget.
    host.view()?.requestMeasure();
    scheduleDeferredRenders();
  }

  /** Re-queue every unmounted markdown placeholder for a deferred re-render. */
  function requeueMarkdown(): void {
    const pending = new Set(deferredRenders.slice(deferredCursor));
    for (const [key, slot] of cells) {
      if (cellViews.has(key) || pending.has(key)) continue;
      if (isPlainCellText(host.slotText(slot)?.toString() ?? '')) continue;
      deferredRenders.push(key);
    }
    scheduleDeferredRenders();
  }

  /** An undo/redo step changed `changed`: repaint the placeholders it wrote. */
  function repaintChanged(changed: ReadonlyMap<unknown, unknown>): void {
    if (!host.doc() || host.readOnly()) return;
    const keys = [...cells]
      .filter(([key, slot]) => !cellViews.has(key) && changed.has(host.slotText(slot)))
      .map(([key]) => key);
    repaintLazy(keys);
  }

  /** Re-render the unmounted placeholders among `keys` from their live text; a mounted
   *  cell is already live through yCollab. */
  function repaintLazy(keys: readonly string[]): void {
    let changed = false;
    for (const key of keys) {
      const slot = cells.get(key);
      if (!slot || cellViews.has(key)) continue;
      // WHY clamped: the find index republishes after the write, so its ranges still name
      // the old text — a range past the new end throws in the static render.
      const length = host.slotText(slot)?.length ?? 0;
      repaintPlaceholder(slot, cellMarksFor(key)?.filter((r) => r.to <= length));
      changed = true;
    }
    if (changed) host.view()?.requestMeasure();
  }

  return {
    cellNotes,
    cellMarksFor,
    applyCellMarks,
    applyCurrentCellMatch,
    refreshNotedPlaceholders,
    scheduleDeferredRenders,
    requeueMarkdown,
    repaintChanged,
    repaintLazy,
    currentMatch: (): CurrentCellMatch | null => currentCellMatch,
    /** A built cell painted `marks` — the baseline applyCellMarks diffs against. */
    trackMarks(key: string, marks: readonly MatchRange[] | undefined): void {
      if (marks) lastCellMarks.set(key, marks);
    },
    forget(key: string): void { lastCellMarks.delete(key); },
    /** Queue a markdown cell that shows raw text until its deferred render. */
    defer(key: string): void { deferredRenders.push(key); },
    /** A fresh build starts. WHY noteMap: the build may follow a remote sync that resolved pending anchors. */
    startBuild(): void {
      noteMap = null;
      lastCellMarks = new Map();
    },
    /** The table DOM is dropped: no deferred pass may touch it. */
    reset(): void {
      if (deferredTimer !== null) clearTimeout(deferredTimer);
      deferredTimer = null;
      deferredRenders = [];
      deferredCursor = 0;
    },
  };
}
