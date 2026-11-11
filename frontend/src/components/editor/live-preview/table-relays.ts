/** Host-view relays into mounted table widgets: find marks, link context, find navigation. */
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { linkContextChanged } from './effects';
import {
  cellMarksByTableField,
  collectAnchorSpans,
  currentCellMatchField,
  setCellMarksByTable,
  type MatchRange,
} from '../find-matches';
import type { RowId, ColId } from './table-block-model';
import { findTableWidget, widgetsByWrap } from './table-widget-registry';

/** Shared empty per-table marks entry — the relay's "this table has no match". */
export const NO_CELL_MARKS: ReadonlyMap<string, readonly MatchRange[]> = new Map();

/** True when two per-cell range lists paint identically (the change detector
 *  applyCellMarks skips re-renders on). */
export function sameRanges(a: readonly MatchRange[] | undefined, b: readonly MatchRange[] | undefined): boolean {
  if (a === b) return true;
  if (!a || !b || a.length !== b.length) return false;
  return a.every((r, i) => r.from === b[i].from && r.to === b[i].to);
}

/**
 * Relay: the combined find index published per-cell marks on the host
 * (`setCellMarksByTable` → `cellMarksByTableField`) — hand each mounted table
 * widget its table's entry. Same shape as cellLinkContextRelay: the index owns
 * the data, the widget owns its cells. Widgets mounting later pull the same
 * field at build time (cellMarksFor), so the relay only covers updates.
 * `currentCellMatchField` (the panel's CURRENT cell match) rides the same fan-out:
 * each widget gets its own table's entry, every other widget gets null.
 */
export const cellMarksRelay = ViewPlugin.define((view: EditorView) => ({
  update(update: ViewUpdate) {
    let marks = false;
    for (const tr of update.transactions) {
      for (const e of tr.effects) if (e.is(setCellMarksByTable)) marks = true;
    }
    // By value, not by effect: the field also clears itself on a host caret move.
    const current = update.startState.field(currentCellMatchField, false)
      !== update.state.field(currentCellMatchField, false);
    if (!marks && !current) return;
    const byTable = marks ? update.state.field(cellMarksByTableField, false) : null;
    const cur = current ? update.state.field(currentCellMatchField, false) : undefined;
    for (const wrap of view.dom.querySelectorAll<HTMLElement>('.cm-table-block-wrap')) {
      const widget = widgetsByWrap.get(wrap);
      if (!widget) continue;
      if (byTable) widget.applyCellMarks(byTable.get(widget.tableId) ?? NO_CELL_MARKS);
      if (current) widget.applyCurrentCellMatch(cur && cur.tableId === widget.tableId ? cur : null);
    }
  },
}));

const nextFrame = (): Promise<void> =>
  new Promise((resolve) => requestAnimationFrame(() => resolve()));

/**
 * Reveal one find match inside a table cell — the Find panel's navigation path
 * (see FindReplacePanel). Resolves true when the match was reached (mounted +
 * selected after measure, or scrolled-to static), false when it is gone (an
 * orphan anchor, or the widget could not mount after the scroll). Off-viewport
 * tables are brought in first: scroll the host to the anchor, give CM6 a frame
 * to mount the widget DOM, then reach through it.
 */
export async function revealCellMatchInView(
  view: EditorView,
  match: { tableId: string; rowId: RowId; colId: ColId; from: number; to: number },
): Promise<boolean> {
  let widget = findTableWidget(view, match.tableId);
  if (!widget) {
    const anchor = collectAnchorSpans(view.state.doc.toString()).find(a => a.id === match.tableId);
    if (!anchor) return false; // orphan anchor — no table to reach
    view.dispatch({ effects: EditorView.scrollIntoView(anchor.from, { y: 'center' }) });
    await nextFrame();
    widget = findTableWidget(view, match.tableId);
    if (!widget) return false;
  }
  // The relay may have run before this widget's DOM existed (the scroll just
  // mounted it) — re-apply the host's current cell match; idempotent.
  const cur = view.state.field(currentCellMatchField, false);
  widget.applyCurrentCellMatch(cur && cur.tableId === match.tableId ? cur : null);
  return widget.revealCellMatch(match.rowId, match.colId, match.from, match.to);
}

/** Relays a host's embed-map refresh (`linkContextChanged`, dispatched to the host view
 *  only) into its mounted cell editors. WHY: a cell renders the same embeds; without the
 *  relay an image dropped into a cell before the map knew its reference stays raw text
 *  until the next selection change. */
export const cellLinkContextRelay = ViewPlugin.define((view) => ({
  update(update) {
    if (update.startState.facet(EditorView.editable) && !update.state.facet(EditorView.editable)) {
      // WHY a microtask: dropping a focused cell publishes the focus slot, and its subscribers must not run inside this host's update.
      queueMicrotask(() => {
        for (const wrap of view.dom.querySelectorAll<HTMLElement>('.cm-table-block-wrap')) {
          widgetsByWrap.get(wrap)?.dropCellEditors();
        }
      });
    }
    if (!update.transactions.some((tr) => tr.effects.some((e) => e.is(linkContextChanged)))) return;
    for (const el of view.dom.querySelectorAll('.cm-table-block-cell-editor > .cm-editor')) {
      EditorView.findFromDOM(el as HTMLElement)?.dispatch({ effects: linkContextChanged.of(null) });
    }
    // WHY the lazy placeholders too: they are copies rendered once, so a cell image whose
    // URL reached the map after the table rendered (a cold open) stayed blank until clicked.
    for (const wrap of view.dom.querySelectorAll<HTMLElement>('.cm-table-block-wrap')) {
      widgetsByWrap.get(wrap)?.rerenderLazyCells();
    }
  },
}));
