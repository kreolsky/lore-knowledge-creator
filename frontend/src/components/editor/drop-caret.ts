/**
 * CM6 drop caret: a vertical caret at a document position, shown while an
 * external pointer drag hovers the editor (reference cards dragged from the
 * panel — useSiblingDragReorder + refDragAdapter). Purely visual: the drag
 * owner resolves the position and calls setEditorDropCaret imperatively; the
 * caret then rides remote edits (mapped through changes) and disappears with
 * the view. Mounted with the document render group (renderExtensions) and in
 * editable cell views — the surfaces a pointer drag can drop into.
 * (see SYSTEM: editor)
 */

import { StateEffect, StateField, type Extension, type Transaction } from '@codemirror/state';
import { EditorView, Decoration, WidgetType } from '@codemirror/view';

const setDropCaret = StateEffect.define<number | null>();

class DropCaretWidget extends WidgetType {
  static readonly instance = new DropCaretWidget();
  override eq(other: DropCaretWidget) { return other === this; }
  override toDOM() {
    const el = document.createElement('span');
    el.className = 'cm-drop-caret';
    el.setAttribute('data-drop-caret', '');
    el.setAttribute('aria-hidden', 'true');
    return el;
  }
  override ignoreEvent() { return false; }
}

/** Keep the caret inside the doc — remote edits may shrink it under the drag. */
function clampCaretPos(pos: number, docLength: number): number {
  return Math.min(Math.max(pos, 0), docLength);
}

const dropCaretField = StateField.define<number | null>({
  create: () => null,
  update(value: number | null, tr: Transaction): number | null {
    // Map through changes so the caret follows the text it pointed at.
    let next = value === null ? null : clampCaretPos(tr.changes.mapPos(value), tr.state.doc.length);
    for (const e of tr.effects) {
      if (e.is(setDropCaret)) next = e.value === null ? null : clampCaretPos(e.value, tr.state.doc.length);
    }
    return next;
  },
  provide: f => EditorView.decorations.from(f, pos => (
    pos === null
      ? Decoration.none
      : Decoration.set([Decoration.widget({ widget: DropCaretWidget.instance, side: -1 }).range(pos)])
  )),
});

// Zero-size inline anchor; the bar is an ::after so it never affects layout.
const dropCaretTheme = EditorView.theme({
  '.cm-drop-caret': {
    position: 'relative',
    display: 'inline-block',
    width: '0',
    height: '0',
  },
  '.cm-drop-caret::after': {
    content: '""',
    position: 'absolute',
    top: '-1.1em',
    left: '-2px',
    width: '4px',
    height: '1.4em',
    background: 'var(--accent)',
    pointerEvents: 'none',
  },
});

/** The drop caret extension — a point widget at the caret position (no syntax
 * ownership; see DECORATION-LAYERS.md "Non-syntax decorations"). */
export function dropCaret(): Extension {
  return [dropCaretField, dropCaretTheme];
}

/** Show the drop caret at `pos` in `view`, or clear it with null. No-op when
 * `pos` is out of range; inert in views without the extension (the effect has
 * no reader there). */
export function setEditorDropCaret(view: EditorView, pos: number | null): void {
  const clamped = pos === null ? null : clampCaretPos(pos, view.state.doc.length);
  view.dispatch({ effects: setDropCaret.of(clamped) });
}
