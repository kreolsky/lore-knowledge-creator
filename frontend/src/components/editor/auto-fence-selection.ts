/**
 * Progressive backtick wrapping (Obsidian-style): each backtick adds a layer
 * around the selection, preserving it. On the third layer, transforms into
 * a fenced code block.
 *
 * `text` → ``text`` → ```\ntext\n```
 *  sel=text  sel=text     sel=text
 *
 * ARCH: Prec.high inputHandler — must run before autoSurround so backtick
 * input with selection is handled here, not by the generic pair wrapper.
 * Purely stateless: detects surrounding backticks positionally each time.
 */
// SYSTEM: auto-fence-selection — progressive backtick wrapping (Obsidian-style)

import { EditorView } from '@codemirror/view';
import { EditorSelection, Prec, type Extension } from '@codemirror/state';

export const autoFenceSelection: Extension = Prec.high(
  EditorView.inputHandler.of((view, _from, _to, text) => {
    if (text !== '`') return false;

    const { state } = view;
    const sel = state.selection.main;
    if (sel.empty) return false;

    const before2 = sel.from >= 2 ? state.sliceDoc(sel.from - 2, sel.from) : '';
    const after2 = sel.to + 2 <= state.doc.length ? state.sliceDoc(sel.to, sel.to + 2) : '';

    // Third backtick: `` on each side → fenced code block.
    if (before2 === '``' && after2 === '``') {
      const content = state.sliceDoc(sel.from, sel.to);
      const insert = '```\n' + content + '\n```';
      const contentStart = sel.from - 2 + 4; // remove ``, add ```\n

      view.dispatch({
        changes: { from: sel.from - 2, to: sel.to + 2, insert },
        selection: EditorSelection.range(contentStart, contentStart + content.length),
      });
      return true;
    }

    const before1 = sel.from >= 1 ? state.sliceDoc(sel.from - 1, sel.from) : '';
    const after1 = sel.to + 1 <= state.doc.length ? state.sliceDoc(sel.to, sel.to + 1) : '';

    // Second backtick: ` on each side → add another layer.
    if (before1 === '`' && after1 === '`') {
      view.dispatch({
        changes: [
          { from: sel.from, insert: '`' },
          { from: sel.to, insert: '`' },
        ],
        selection: EditorSelection.range(sel.from + 1, sel.to + 1),
      });
      return true;
    }

    // First backtick: wrap with single backticks.
    const content = state.sliceDoc(sel.from, sel.to);
    const insert = '`' + content + '`';

    view.dispatch({
      changes: { from: sel.from, to: sel.to, insert },
      selection: EditorSelection.range(sel.from + 1, sel.from + 1 + content.length),
    });
    return true;
  })
);
