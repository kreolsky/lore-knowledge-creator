/**
 * Smart quote pairing: insert a paired quote (with caret between) only when
 * the caret sits at a word boundary on both sides — i.e. no word character
 * touches it on the left (closing context) AND no word character touches it
 * on the right (opening-against-word context). Otherwise insert a single
 * character so users can naturally close `word"` or open `"word` without
 * fighting auto-pairs.
 *
 * Backtick is intentionally EXCLUDED from PAIRS — it is handled by
 * auto-fence-selection.ts (progressive wrapping), auto-fence.ts (fenced
 * code block), and closeBrackets() (inline code pairing).
 *
 * ARCH: Prec.high inputHandler — must run before closeBrackets() to override
 * its unconditional pair insertion. Registered AFTER autoFence.
 */
// SYSTEM: smart-quotes — context-aware paired quote insertion

import { EditorView } from '@codemirror/view';
import { EditorSelection, Prec, type Extension } from '@codemirror/state';

const PAIRS: Record<string, string> = {
  '"': '"',
  "'": "'",
  '«': '»',
  '\u201C': '\u201D',
  '\u2018': '\u2019',
};

const WORD_CHAR = /[\p{L}\p{N}_]/u;

export const smartQuotes: Extension = Prec.high(
  EditorView.inputHandler.of((view, from, to, text) => {
    const closing = PAIRS[text];
    if (!closing) return false;

    const { state } = view;
    if (from !== to) return false;
    if (state.selection.ranges.length !== 1 || !state.selection.main.empty) return false;

    const docLen = state.doc.length;
    const charBefore = from > 0 ? state.sliceDoc(from - 1, from) : '';
    const charAfter = to < docLen ? state.sliceDoc(to, to + 1) : '';
    const wordOnLeft = charBefore !== '' && WORD_CHAR.test(charBefore);
    const wordOnRight = charAfter !== '' && WORD_CHAR.test(charAfter);
    const treatAsSingle = wordOnLeft || wordOnRight;

    if (treatAsSingle) {
      view.dispatch({
        changes: { from, to, insert: text },
        selection: EditorSelection.cursor(from + text.length),
        userEvent: 'input.type',
      });
      return true;
    }

    view.dispatch({
      changes: { from, to, insert: text + closing },
      selection: EditorSelection.cursor(from + text.length),
      userEvent: 'input.type',
    });
    return true;
  })
);
