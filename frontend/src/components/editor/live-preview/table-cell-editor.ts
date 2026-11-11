/**
 * The extension list of a table cell's nested CM6 EditorView (see SYSTEM: table-block in
 * `table-block-widget.ts`): live-preview rendering, the yCollab binding to the cell's
 * Y.Text, focus-driven reveal, cell navigation keys and the host re-measure.
 */
import { EditorView, keymap } from '@codemirror/view';
import { Prec, Compartment, type Extension } from '@codemirror/state';
import { insertNewlineAndIndent } from '@codemirror/commands';
import type * as Y from 'yjs';
import { claimNestedView, getViewEntity } from '../../../editor/active-editor';
import { createYjsExtension } from '../../../collab/yjs-binding';
import { editableCellExtensions } from '../render-bundle';
import { revealAtCursor } from './effects';
import { boundYText, cellNoteExtension } from '../note-anchor-wiring';
import { viewEntity } from './table-doc-source';

/** What the cell editor needs from its widget. */
export interface CellEditorDeps {
  cellText: Y.Text;
  undoManager: Y.UndoManager | null;
  /** The host (document) view, read lazily: CM6 may re-run toDOM on a reused widget. */
  hostView: () => EditorView | null;
  tabStep: (step: 1 | -1) => boolean;
  backspaceRow: (cell: EditorView) => boolean;
}

export function cellEditorExtensions(deps: CellEditorDeps): Extension[] {
  const ytext = deps.cellText;
  const { undoManager, hostView } = deps;
  const host = hostView();
  // INVARIANT: a cell reveals raw markdown at the caret ONLY while focused; on blur it
  // renders fully formatted (like a static table cell). Why: an unfocused cell left with
  // the caret inside `**bold**` would otherwise keep showing the raw `**` markers. Driven
  // by a focus-toggled compartment that overrides the cell's base revealAtCursor.
  const revealComp = new Compartment();
  return [
    editableCellExtensions(),
    // A cell belongs to its host's entity (image drop, paste placement).
    viewEntity.of(host ? getViewEntity(host) : null),
    // Offsets read from this view index the cell's Y.Text: notes anchor over it and paint here only.
    boundYText.of(ytext),
    cellNoteExtension(() => {
      const v = hostView();
      return v ? getViewEntity(v) ?? undefined : undefined;
    }),
    revealComp.of(revealAtCursor.of(false)),
    EditorView.domEventHandlers({
      // WHY echo the selection: the live-preview StateFields rebuild on
      // docChanged/selection/syntax change but NOT on a bare reconfigure, so a
      // facet-only toggle would leave the decorations stale (formatting stuck
      // revealed). Re-setting the current range forces the rebuild that re-reads
      // revealAtCursor.
      // Claimed on the DOM focus event, like the document (editor-extension-bundle):
      // the cell is the active editor before anything it dispatches is published.
      focus: (_e, v) => { claimNestedView(v); v.dispatch({ effects: revealComp.reconfigure(revealAtCursor.of(true)), selection: v.state.selection }); return false; },
      // WHY deferred: any dispatch re-checks focus, so a synchronous one here reports
      // focus loss mid-transfer — before focus lands in the image toolbar's field,
      // which then closed under the click. CM6 defers its own focus notice the same way.
      blur: (_e, v) => {
        setTimeout(() => {
          if (v.dom.isConnected && !v.hasFocus) v.dispatch({ effects: revealComp.reconfigure(revealAtCursor.of(false)), selection: v.state.selection });
        }, 0);
        return false;
      },
    }),
    createYjsExtension(ytext, undoManager),
    cellNavKeymap(deps),
    cellMeasureListener(host),
  ];
}

/** Re-measures the host when a cell's geometry changes. The cell's ACTIVE-editor claim
 * is its focus handler (cellEditorExtensions); blur hands nothing back, exactly like the
 * document — focus moving into the toolbar's size field keeps the cell active, and
 * whichever editor takes focus next claims it itself (destroyCellView covers a cell view
 * that dies while active). */
function cellMeasureListener(outer: EditorView | null): Extension {
  return EditorView.updateListener.of((update) => {
    // WHY: a cell's height can grow AFTER the widget was first measured
    // (yCollab fills/wraps content asynchronously). CM6 reserves the line's height from a
    // one-time DOM measure, so without forcing a re-measure the grown table overflows its
    // reserved box and rows below the first overlap — and steal clicks for the next document
    // line. Re-measure the OUTER view when a cell's GEOMETRY changes. Why only geometryChanged,
    // not docChanged: a content edit that does not change the cell's height needs no re-measure,
    // and firing requestMeasure on every keystroke re-runs the outer editor's full
    // measurement pass (O(doc)) — a per-keystroke cost that compounds across tables. CM6 sets
    // geometryChanged precisely when a cell's measured height actually changes.
    if (update.geometryChanged) outer?.requestMeasure();
  });
}

/**
 * Tab/Shift-Tab navigate between cells; Tab on the bottom-right cell appends a row.
 * Enter/Shift-Enter insert an intra-cell line. Backspace at the start of an empty row's
 * first cell removes the row (see `backspaceRow`).
 * INVARIANT: Enter does not create a table row; Tab creates one only from the bottom-right cell.
 * Why: Tab past the end is the universal "next row" gesture; elsewhere keystrokes stay
 * inside the cell.
 * WHY no arrow into a cell from the document and no Esc out of one: operator ruling, a
 * cell is entered by click and left by click.
 */
function cellNavKeymap(deps: CellEditorDeps): Extension {
  return Prec.highest(keymap.of([
    { key: 'Tab', run: () => deps.tabStep(1), shift: () => deps.tabStep(-1) },
    { key: 'Shift-Enter', run: insertNewlineAndIndent },
    { key: 'Backspace', run: (v) => deps.backspaceRow(v) },
  ]));
}
