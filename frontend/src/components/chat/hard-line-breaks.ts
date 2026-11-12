/** Keep a typed message's single line breaks when it is rendered as markdown. */

const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})/;

/**
 * Turn every single newline between two non-empty lines into a CommonMark hard break
 * (two trailing spaces), outside fenced code. Blank lines (paragraphs) are untouched.
 *
 * WHY: CommonMark folds a single newline into a space, so a multi-line message typed in
 * the composer would render as one run-on paragraph; the user's line structure is
 * content, not formatting noise.
 */
export function hardLineBreaks(md: string): string {
  const lines = md.split('\n');
  let fence: string | null = null;
  return lines.map((line, i) => {
    const marker = line.match(FENCE_RE)?.[1][0];
    if (marker) {
      if (fence === null) fence = marker;
      else if (marker === fence) fence = null;
      return line;
    }
    const next = lines[i + 1];
    if (fence !== null || next === undefined || !line.trim() || !next.trim()) return line;
    return `${line}  `;
  }).join('\n');
}
