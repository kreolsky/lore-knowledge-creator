/** Splice text into a textarea's value at its caret (replacing any selection), space-padded against adjacent words. */

export interface CaretInsert {
  text: string;
  caret: number;
}

/**
 * Insert `insert` into `value` at the textarea's selection. A null textarea appends, so a
 * composer with no mounted input still keeps the text. A space is added on either side only
 * where the neighbouring character is not already whitespace. `caret` is the position right
 * after the inserted text, for the caller to restore once the controlled value re-renders.
 */
export function insertAtCaret(value: string, insert: string, el: HTMLTextAreaElement | null): CaretInsert {
  const start = el ? Math.min(el.selectionStart, value.length) : value.length;
  const end = el ? Math.min(Math.max(el.selectionEnd, start), value.length) : value.length;
  const before = value.slice(0, start);
  const after = value.slice(end);
  const lead = before && !/\s$/.test(before) ? ' ' : '';
  const trail = after && !/^\s/.test(after) ? ' ' : '';
  const head = before + lead + insert;
  return { text: head + trail + after, caret: head.length };
}
