/** Module-level registries of mounted table widgets: wrap → widget, lazy cell mounters, pending focus. */
import type { EditorView } from '@codemirror/view';
import type { TableBlockWidget } from './table-block-widget';

/** tableId queued by `insertTable` to receive focus when its widget first mounts. */
let pendingFocusTableId: string | null = null;
export function requestTableFocus(tableId: string): void {
  pendingFocusTableId = tableId;
}

/** True once for the widget of the table queued by `requestTableFocus`; clears the queue. */
export function takeTableFocus(tableId: string): boolean {
  if (pendingFocusTableId !== tableId) return false;
  pendingFocusTableId = null;
  return true;
}

/** Cell editor host → its click mount path (`ensureCellEditor`), so a drag hovering a
 * never-focused cell can mount it (drop-target.editorDropPoint). WeakMap: a structural
 * patch replaces the host, and the dead one must be collectable. */
export const lazyCellMounters = new WeakMap<HTMLElement, () => EditorView | null>();

/** Mount (or return) the cell editor of a placeholder host; null for a host that is
 * stale after a patch or belongs to no editable cell. */
export function mountLazyCell(host: Element): EditorView | null {
  const mount = lazyCellMounters.get(host as HTMLElement);
  return mount ? mount() : null;
}

/** Wrap → its widget, so a host's editability drop can reach the mounted cells. */
export const widgetsByWrap = new WeakMap<HTMLElement, TableBlockWidget>();


/** The live widget for a table id, or null — CM6 renders a block widget's DOM
 *  only near the viewport, so a table scrolled out of view has no wrap in
 *  view.dom (and an unanchored/missing table never had one). */
export function findTableWidget(view: EditorView, tableId: string): TableBlockWidget | null {
  const wrap = view.dom.querySelector<HTMLElement>(
    `.cm-table-block-wrap[data-table-id="${CSS.escape(tableId)}"]`);
  return wrap ? widgetsByWrap.get(wrap) ?? null : null;
}
