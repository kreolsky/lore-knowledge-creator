/** Markdown serialization for chat clarify questions (quote + question). */

/** Serialize a selected fragment into a markdown blockquote followed by the question. */
export function clarifyBlock(quote: string, question: string): string {
  const quoted = quote.split('\n').map(line => `> ${line}`).join('\n');
  return `${quoted}\n\n${question}`;
}
