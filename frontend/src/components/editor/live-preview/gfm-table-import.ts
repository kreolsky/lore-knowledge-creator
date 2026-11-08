/**
 * GFM markdown → table-object import (the paste/drop path).
 *
 * SYSTEM: table-block — the UI-side GFM→object conversion; the backend twin is
 * table_serialize.py (parse_gfm_table + import_gfm_tables), which every markdown import
 * and agent-authored body runs. The two scanners agree byte-for-byte: fence skip, outer
 * pipes optional (`_split_row`), image-embed `|` never splits a cell, and the
 * no-outer-pipe block rule. The shared fixture in gfm-table-import.test.ts /
 * tests/backend/test_table_serialize.py enforces it. Tables typed by hand in the editor
 * stay plain GFM.
 *
 * INVARIANT: cell-text parsing is byte-for-byte the inverse of the serializer — `<br>` →
 * newline, `\|` → literal pipe, exactly one padding space stripped per side. Why: an
 * import must not corrupt content; the model is the source of truth (see table_serialize).
 */
import type * as Y from 'yjs';
import { createTable, tableAnchor } from './table-block-model';

// Split a GFM row on a `|` NOT preceded by a backslash escape (mirror of backend _CELL_SPLIT).
const CELL_SPLIT = /(?<!\\)\|/;
// An image-embed span (`![alt|WxH](ref:id)`) — its `|` is the sized-image separator,
// never a cell delimiter (mirror of backend _IMAGE_EMBED).
const IMAGE_EMBED = /!\[[^\]]*\]\([^)]*\)/g;
const FENCE_OPEN = /^(\s*)(`{3,}|~{3,})/;
const FENCE_ONLY = /^(`{3,}|~{3,})\s*$/;

/** Per-line fence state (mirror of backend markdown_normalize.fence_mask): opener, body
 * and closer are masked; an unclosed fence masks to EOF. */
function fenceMask(lines: string[]): boolean[] {
  const mask = new Array<boolean>(lines.length).fill(false);
  let inFence = false;
  let fenceChar = '';
  lines.forEach((line, idx) => {
    if (!inFence) {
      const m = FENCE_OPEN.exec(line);
      if (m) {
        inFence = true;
        fenceChar = m[2][0];
        mask[idx] = true;
      }
      return;
    }
    mask[idx] = true;
    const stripped = line.trimStart();
    if (FENCE_ONLY.test(stripped) && stripped[0] === fenceChar) inFence = false;
  });
  return mask;
}

function unescapeCell(wire: string): string {
  return wire.replace(/<br\s*\/?>/gi, '\n').replace(/\\\|/g, '|');
}

/** Split one row into raw cell segments (mirror of backend _split_row): drop ONE optional
 * leading `|` and ONE optional unescaped trailing `|`, split on unescaped pipes outside
 * image-embed spans. */
function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const embeds = s.match(IMAGE_EMBED);
  if (!embeds) return s.split(CELL_SPLIT);
  let k = 0;
  return s
    .replace(IMAGE_EMBED, '\x00')
    .split(CELL_SPLIT)
    // WHY: `\x00` is the backend's embed placeholder too (never a literal in text content).
    // eslint-disable-next-line no-control-regex
    .map((seg) => seg.replace(/\x00/g, () => embeds[k++]));
}

/** The line holds a cell-delimiter `|` outside image-embed spans. */
function rowHasPipe(line: string): boolean {
  return CELL_SPLIT.test(line.replace(IMAGE_EMBED, '\x00'));
}

function hasLeadingPipe(line: string): boolean {
  return line.trim().startsWith('|');
}

/** True for a GFM header-separator row (`| --- | :--: | …`, outer pipes optional). */
function isSeparatorRow(line: string): boolean {
  const cells = splitRow(line).map((c) => c.trim());
  // Require ≥1 NON-EMPTY dash cell. Why: `[].every()` is true, so without this an all-empty
  // data row (`|   |   |`) is misclassified as the separator and silently dropped on import.
  const dashCells = cells.filter((c) => c !== '');
  return dashCells.length > 0 && dashCells.every((c) => /^:?-+:?$/.test(c));
}

/** Parse one row into cell bodies (strips exactly one padding space per side). */
function parseRow(line: string): string[] {
  return splitRow(line).map((seg) => {
    if (seg.startsWith(' ')) seg = seg.slice(1);
    if (seg.endsWith(' ')) seg = seg.slice(0, -1);
    return unescapeCell(seg);
  });
}

/** Mirror of backend _separator_matches_header: a pipe-prefixed separator under a
 * leading-pipe header; under a no-pipe header a separator HOLDING a pipe with an equal
 * cell count (`a\n---` and `a | b\n---` stay setext headings). */
function separatorMatchesHeader(lines: string[], i: number, mask: boolean[], leading: boolean): boolean {
  if (i + 1 >= lines.length || mask[i + 1] || !isSeparatorRow(lines[i + 1])) return false;
  const sep = lines[i + 1];
  if (leading) return hasLeadingPipe(sep);
  return rowHasPipe(sep) && splitRow(lines[i]).length === splitRow(sep).length;
}

/** Mirror of backend _block_end: index of the block's LAST line from header `i`. */
function blockEnd(lines: string[], i: number, mask: boolean[], leading: boolean): number {
  const isBody = leading
    ? (l: string) => hasLeadingPipe(l)
    : (l: string) => l.trim() !== '' && rowHasPipe(l);
  let j = i + 1;
  while (j + 1 < lines.length && !mask[j + 1] && isBody(lines[j + 1])) j++;
  return j;
}

/** Mirror of backend _collect_table_block: the matrix + next index, or null when line `i`
 * does not START a table. Fenced lines never start or continue a block. */
function collectTableBlock(lines: string[], i: number, mask: boolean[]): { matrix: string[][]; next: number } | null {
  const line = lines[i];
  if (mask[i] || !line.trim() || isSeparatorRow(line)) return null;
  const leading = hasLeadingPipe(line);
  if (!leading && !rowHasPipe(line)) return null;
  if (!separatorMatchesHeader(lines, i, mask, leading)) return null;
  const j = blockEnd(lines, i, mask, leading);
  const matrix = lines.slice(i, j + 1).filter((l) => !isSeparatorRow(l)).map(parseRow);
  return matrix.length > 0 ? { matrix, next: j + 1 } : null;
}

/**
 * Replace every GFM table block in `markdown` with a fresh table-object anchor, creating
 * the backing model in `doc`. Returns the rewritten text + the number of tables converted.
 *
 * A block = a header row immediately followed by a separator row, then the body run (outer
 * pipes optional — see collectTableBlock). Fenced code and prose are left untouched.
 */
export function importGfmTables(doc: Y.Doc, markdown: string, label: string): { text: string; count: number } {
  const lines = markdown.split('\n');
  const mask = fenceMask(lines);
  const out: string[] = [];
  let count = 0;
  let i = 0;
  while (i < lines.length) {
    const block = collectTableBlock(lines, i, mask);
    if (block) {
      out.push(tableAnchor(label, createTable(doc, block.matrix)));
      count++;
      i = block.next;
      continue;
    }
    out.push(lines[i]);
    i++;
  }
  return { text: out.join('\n'), count };
}
