/**
 * Auto-close fenced code blocks: typing the third backtick (completing ```)
 * at the start of a line inserts a closing ``` fence on the next line and
 * places the cursor between the two fences.
 *
 * ARCH: Prec.high inputHandler — must run before closeBrackets() to intercept
 * the third backtick before it gets treated as a regular bracket pair.
 */
// SYSTEM: auto-fence — auto-close fenced code blocks on third backtick

import { EditorView } from '@codemirror/view';
import { Prec, type Extension } from '@codemirror/state';

export const autoFence: Extension = Prec.high(
  EditorView.inputHandler.of((view, from, to, text) => {
    if (text !== '`') return false;

    const { state } = view;
    const line = state.doc.lineAt(from);
    const prefix = state.sliceDoc(line.from, from);

    // Only trigger when the line so far is exactly `` (two backticks).
    if (prefix !== '``') return false;

    // Don't trigger if there's non-whitespace after cursor (e.g. inline code).
    const suffix = state.sliceDoc(to, line.to);
    if (suffix.trim() !== '') return false;

    // Insert: third backtick + newline + newline + closing fence.
    const insert = '`\n\n```';

    view.dispatch({
      changes: { from, to, insert },
      selection: { anchor: from + 2 }, // cursor on empty line between fences
    });

    return true;
  })
);
