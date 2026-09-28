/**
 * Auto-surround extension: wraps non-empty selections with bracket/quote pairs.
 *
 * ARCH: Prec.high inputHandler — must run before closeBrackets() to intercept
 * bracket/quote input when text is selected. closeBrackets would keep the
 * selection inside; this places cursor after the closing character instead.
 */
// SYSTEM: auto-surround — wrap selection with bracket/quote pairs

import { EditorView } from '@codemirror/view';
import { EditorSelection, Prec, type Extension } from '@codemirror/state';

const PAIRS: Record<string, string> = {
  '(': ')',
  '[': ']',
  '{': '}',
  '"': '"',
  "'": "'",
};

export const autoSurround: Extension = Prec.high(
  EditorView.inputHandler.of((view, _from, _to, text) => {
    const closing = PAIRS[text];
    if (!closing) return false;

    const { state } = view;
    if (!state.selection.ranges.some(r => !r.empty)) return false;

    view.dispatch(
      state.changeByRange(range => {
        if (range.empty) {
          return {
            range: EditorSelection.cursor(range.from + 1),
            changes: { from: range.from, insert: text },
          };
        }
        const content = state.sliceDoc(range.from, range.to);
        const insert = text + content + closing;
        return {
          range: EditorSelection.cursor(range.from + insert.length),
          changes: { from: range.from, to: range.to, insert },
        };
      })
    );
    return true;
  })
);
