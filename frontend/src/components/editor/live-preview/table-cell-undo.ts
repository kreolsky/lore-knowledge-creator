/**
 * Cell caret on the document-wide undo stack: which table cell a step was typed in, so a
 * Ctrl+Z / redo of that step puts the caret back in that cell — mounting it first when it
 * is only a static placeholder now.
 *
 * WHY own meta and not y-codemirror's: y-codemirror keys its selection meta by the binding
 * plugin INSTANCE, so it restores only a cell whose editor is still the one that typed —
 * a lazily (re)mounted cell, or one undone from the host, would get nothing. This meta is
 * keyed by ids `(tableId, rowId, colId)` plus a Yjs relative position, which survive both.
 */

import * as Y from 'yjs';
import type { ColId, RowId } from './table-block-model';

export interface CellCaret { tableId: string; rowId: RowId; colId: ColId; pos: Y.RelativePosition }

/** A live table widget, as seen by the undo stack. */
export interface CellCaretHost {
  readonly tableId: string;
  /** The ydoc the widget renders (null before it binds). */
  boundDoc(): Y.Doc | null;
  /** The FOCUSED mounted cell, when its Y.Text is among `changed`, as a caret. */
  focusedCaret(changed: ReadonlyMap<unknown, unknown>): CellCaret | null;
  /** Mount the cell if needed, place the caret, focus it. */
  restoreCaret(caret: CellCaret): void;
  /** On screen now — the inline widget hidden behind an open table view is not. */
  isVisible(): boolean;
}

const CELL_CARET = Symbol('table-cell-caret');
const hosts = new Set<CellCaretHost>();
const wired = new WeakSet<Y.UndoManager>();

export function registerCellCaretHost(host: CellCaretHost): () => void {
  hosts.add(host);
  return () => { hosts.delete(host); };
}

function focusedCaret(doc: Y.Doc, changed: ReadonlyMap<unknown, unknown>): CellCaret | null {
  for (const host of hosts) {
    if (host.boundDoc() !== doc) continue;
    const caret = host.focusedCaret(changed);
    if (caret) return caret;
  }
  return null;
}

/**
 * Attach the cell-caret meta to `manager` (once per manager; it dies with it).
 * A NEW step records the focused cell it was typed in; the step an undo/redo pushes onto
 * the opposite stack inherits the popped step's caret, so redo lands in the same cell.
 */
export function wireCellCaret(manager: Y.UndoManager): void {
  if (wired.has(manager)) return;
  wired.add(manager);
  manager.on('stack-item-added', ({ stackItem, changedParentTypes }) => {
    if (stackItem.meta.has(CELL_CARET)) return;
    const caret: CellCaret | null | undefined = manager.undoing || manager.redoing
      ? manager.currStackItem?.meta.get(CELL_CARET)
      : focusedCaret(manager.doc, changedParentTypes);
    if (caret) stackItem.meta.set(CELL_CARET, caret);
  });
  manager.on('stack-item-popped', ({ stackItem }) => {
    const caret = stackItem.meta.get(CELL_CARET) as CellCaret | undefined;
    if (!caret) return;
    const candidates = [...hosts].filter((h) => h.tableId === caret.tableId && h.boundDoc() === manager.doc);
    (candidates.find((h) => h.isVisible()) ?? candidates[0])?.restoreCaret(caret);
  });
}
