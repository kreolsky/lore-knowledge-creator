/** Markdown serialization for a quote sent into the chat draft (quote [+ source] [+ question]). */

/** Serialize a quoted fragment as a markdown blockquote, then its source line, then the question. */
export function clarifyBlock(quote: string, source?: string, question?: string): string {
  const quoted = quote.split('\n').map(line => `> ${line}`).join('\n');
  // WHY the blank line before the source: a line right after `> …` is a lazy
  // continuation in CommonMark and would render inside the quote, glued to its last line.
  return [quoted, source && `— ${source}`, question].filter(Boolean).join('\n\n');
}
