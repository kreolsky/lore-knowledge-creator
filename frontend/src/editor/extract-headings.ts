/** Extract h1–h4 headings from markdown, skipping fenced code blocks.
 *
 * This is a CLIENT-SIDE mirror of backend deps.py::extract_headings. The two must
 * agree exactly on level/text/line so the live (ephemeral) headings and the
 * persisted server headings stay interchangeable for the TOC and scroll-to-line.
 *
 * Algorithm: iterate lines, track fenced blocks (^(`{3,}|~{3,}), matching the
 * fence char + length to close), skip in-fence lines, match ^(#{1,4})\s+(.+)$
 * outside fences, emit { level, text, line: i+1 }.
 *
 * Two entry points:
 *  - extractHeadings(content: string)  — split-on-newline form, used by tests and
 *    any caller with a plain string.
 *  - extractHeadingsFromLines(getLine, count) — used by the CM6 plugin to iterate
 *    the live Text in O(1) per line, avoiding a full-doc toString()+split()
 *    reallocation on every keystroke.
 */
// SYSTEM: extract-headings — client-side heading parser (mirror of backend)

import type { HeadingItem } from '../types';

const FENCE_RE = /^(`{3,}|~{3,})/;
const HEADING_RE = /^(#{1,4})\s+(.+)$/;

function buildHeadings(lineCount: number, getLine: (i: number) => string): HeadingItem[] {
  const items: HeadingItem[] = [];
  let inFenced = false;
  let fenceChar = '';
  let fenceLen = 0;

  for (let i = 0; i < lineCount; i++) {
    const line = getLine(i);
    const fenceMatch = line.match(FENCE_RE);
    if (fenceMatch) {
      const marker = fenceMatch[1];
      if (!inFenced) {
        inFenced = true;
        fenceChar = marker[0];
        fenceLen = marker.length;
      } else if (line[0] === fenceChar && marker.length >= fenceLen) {
        inFenced = false;
        fenceChar = '';
        fenceLen = 0;
      }
      continue;
    }
    if (inFenced) continue;

    const headingMatch = line.match(HEADING_RE);
    if (headingMatch) {
      items.push({
        level: headingMatch[1].length,
        text: headingMatch[2],
        line: i + 1,
      });
    }
  }
  return items;
}

export function extractHeadings(content: string): HeadingItem[] {
  const lines = content.split('\n');
  return buildHeadings(lines.length, i => lines[i]);
}

/** O(1)-per-line variant for the live CM6 doc — avoids toString()+split(). */
export function extractHeadingsFromLines(lineCount: number, getLine: (i: number) => string): HeadingItem[] {
  return buildHeadings(lineCount, getLine);
}

