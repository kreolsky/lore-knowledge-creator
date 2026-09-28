/**
 * Custom list continuation handler for Enter key in Markdown lists.
 *
 * Wraps @codemirror/lang-markdown's insertNewlineContinueMarkupCommand with
 * nonTightLists:false to enable proper dedent-on-empty behavior:
 *   1. Enter on non-empty bullet → new bullet at same nesting level
 *   2. Enter on empty bullet (level 2+) → dedent one level, keep marker
 *   3. Enter on empty bullet (level 1) → remove marker entirely
 *
 * Requires indentUnit = "\t" so normalizeIndent converts spaces to tabs,
 * matching the tab-based indentation used by indentLines/outdentLines.
 */
// ARCH: Replaces built-in markdownKeymap Enter handler with nonTightLists:false config.
// INVARIANT: indentUnit must be "\t" wherever this extension is used.  Why: Enter handling is computed against a tab indent unit; a different unit desynchronizes the list-mark offset and breaks continuation.

import { keymap, type Command } from '@codemirror/view';
import { insertNewlineContinueMarkupCommand, deleteMarkupBackward } from '@codemirror/lang-markdown';
import { Prec, type Extension } from '@codemirror/state';

const continueList = insertNewlineContinueMarkupCommand({ nonTightLists: false });

// WHY: For checkbox lines at end-of-line, the built-in continuation produces
// `- [ ]` without a trailing space — the Lezer task-list grammar then fails to
// recognise the TaskMarker, the live-preview widget collapses, and the cursor
// renders before the bullet. We short-circuit and emit the continuation
// ourselves with the trailing space in a single dispatch (so the live-preview
// decorations rebuild only once and the final cursor lands past the widget).
const CHECKBOX_LINE_RE = /^(\s*)(?:[-*+])\s+\[[ xX]\](?:\s.*|\s*)$/;

export const continueListWithSpace: Command = (target) => {
  const state = target.state;
  const sel = state.selection.main;
  if (!sel.empty) return continueList(target);
  const line = state.doc.lineAt(sel.head);
  if (sel.head !== line.to) return continueList(target);
  const m = line.text.match(CHECKBOX_LINE_RE);
  if (!m) return continueList(target);
  const indent = m[1];
  // Empty checkbox at end-of-line: dedent to plain newline (level 2+) or
  // remove marker (level 1). Defer to the built-in for that branch.
  const afterMarker = line.text.slice(indent.length + 5).replace(/^\s/, '');
  if (afterMarker.trim().length === 0) return continueList(target);
  const insert = `\n${indent}- [ ] `;
  target.dispatch(state.update({
    changes: { from: line.to, insert },
    selection: { anchor: line.to + insert.length },
    scrollIntoView: true,
  }));
  return true;
};

export const listContinuationExtension: Extension = Prec.high(keymap.of([
  { key: 'Enter', run: continueListWithSpace },
  { key: 'Backspace', run: deleteMarkupBackward },
]));
