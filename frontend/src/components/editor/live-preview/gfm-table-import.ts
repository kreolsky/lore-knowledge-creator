/**
 * GFM markdown → table-object import (the paste/import path).
 *
 * SYSTEM: table-block — the ONE sanctioned GFM→object conversion on the frontend (mirror
 * of backend `table_serialize.parse_gfm_table`). Typed/authored pipe tables stay plain
 * GFM; only an *imported* GFM table is upgraded into an editable table object so the user
 * can edit it. Detects a GFM table block in markdown, creates a `tables` model per block,
 * and splices an `![label](table:id)` anchor in its place.
 *
 * INVARIANT: cell-text parsing is byte-for-byte the inverse of the serializer — `<br>` →
 * newline, `\|` → literal pipe, exactly one padding space stripped per side. Why: an
 * import must not corrupt content; the model is the source of truth (see table_serialize).
 */
import type * as Y from 'yjs';
import { createTable, tableAnchor } from './table-block-model';

// Split a GFM row on a `|` NOT preceded by a backslash escape (mirror of backend _CELL_SPLIT).
const CELL_SPLIT = /(?<!\\)\|/;

function unescapeCell(wire: string): string {
  return wire.replace(/<br\s*\/?>/gi, '\n').replace(/\\\|/g, '|');
}

function isPipeLine(line: string): boolean {
  return line.trim().startsWith('|');
}

/** True for a GFM header-separator row (`| --- | :--: | …`). */
function isSeparatorRow(line: string): boolean {
  const s = line.trim();
  if (!s.startsWith('|')) return false;
  const cells = s.slice(1, -1).split(CELL_SPLIT).map((c) => c.trim());
  // Require ≥1 NON-EMPTY dash cell. Why: `[].every()` is true, so without this an all-empty
  // data row (`|   |   |`) is misclassified as the separator and silently dropped on import.
  const dashCells = cells.filter((c) => c !== '');
  return dashCells.length > 0 && dashCells.every((c) => /^:?-+:?$/.test(c));
}

/** Parse one `| a | b |` row into cell bodies (strips exactly one padding space per side). */
function parseRow(line: string): string[] {
  const inner = line.trim().slice(1, -1); // drop leading/trailing pipe
  return inner.split(CELL_SPLIT).map((seg) => {
    if (seg.startsWith(' ')) seg = seg.slice(1);
    if (seg.endsWith(' ')) seg = seg.slice(0, -1);
    return unescapeCell(seg);
  });
}

/**
 * Replace every GFM table block in `markdown` with a fresh table-object anchor, creating
 * the backing model in `doc`. Returns the rewritten text + the number of tables converted.
 *
 * A block = a pipe-line header immediately followed by a separator row, then any run of
 * pipe-lines. Prose (and pipes not forming a table) are left untouched.
 */
export function importGfmTables(doc: Y.Doc, markdown: string, label: string): { text: string; count: number } {
  const lines = markdown.split('\n');
  const out: string[] = [];
  let count = 0;
  let i = 0;

  while (i < lines.length) {
    const headerIsTable =
      isPipeLine(lines[i]) &&
      !isSeparatorRow(lines[i]) &&
      i + 1 < lines.length &&
      isSeparatorRow(lines[i + 1]);

    if (headerIsTable) {
      let j = i + 1;
      while (j + 1 < lines.length && isPipeLine(lines[j + 1])) j++;
      const matrix = lines
        .slice(i, j + 1)
        .filter((l) => !isSeparatorRow(l))
        .map(parseRow);
      if (matrix.length > 0) {
        const id = createTable(doc, matrix);
        out.push(tableAnchor(label, id));
        count++;
        i = j + 1;
        continue;
      }
    }

    out.push(lines[i]);
    i++;
  }

  return { text: out.join('\n'), count };
}
