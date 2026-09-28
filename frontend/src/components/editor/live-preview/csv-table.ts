/**
 * CSV/TSV → table-matrix import core (the delimited-text import path).
 *
 * SYSTEM: table-block — the ONE sanctioned CSV→matrix conversion on the frontend (sibling
 * of `gfm-table-import.ts`, which handles the markdown pipe-table format). A `.csv`/`.tsv`
 * file is parsed into a `string[][]` matrix that feeds `createTable`; no `Y.Doc` here (pure
 * functions — mirrors the gfm import's separation of parse vs. CRDT write).
 *
 * INVARIANT: `parseCsv` is the inverse of `serializeTableCsv` for any matrix whose serialized
 * body is NON-empty. Why: an import→edit→export round-trip must not corrupt cell content; the
 * cell-text is the source of truth (see table-block model INVARIANT). The one exception is an
 * ALL-EMPTY matrix (e.g. a 1×1 `[['']]` table): it serializes to a BOM-only body, which parses
 * back to `[]` — an empty body is indistinguishable from an empty file, so the round-trip is
 * intentionally undefined there (such a table cannot be re-imported). Quoted fields follow
 * RFC-4180: `""` → `"`, embedded delimiter/newline/quote allowed inside
 * a quoted field, line terminator is CRLF on the wire (LF accepted on input).
 */

/** Reject an import above this many rows (CRDT-safety: each cell is a Y.Text). */
export const MAX_TABLE_IMPORT_ROWS = 5000;
/** Reject an import above this many columns (max-width across all rows). */
export const MAX_TABLE_IMPORT_COLS = 200;
/**
 * Reject an import above this many CELLS total (rows × cols). Why: `createTable` allocates a
 * Y.Map + Y.Text per cell synchronously in one transact; 100k cells is a sub-second build,
 * while the row/col caps alone would permit a 1M-cell (multi-second) import freeze.
 */
export const MAX_TABLE_IMPORT_CELLS = 100_000;

const BOM = '\uFEFF';

/**
 * Pick the delimiter for a `.csv` by counting `,` `;` `\t` on the first non-empty line and
 * returning the most frequent one (covers European `;`-CSV). Falls back when none is present.
 */
export function sniffDelimiter(text: string, fallback: string): string {
  const lines = text.replace(/^\uFEFF/, '').split(/\r\n|\r|\n/);
  const first = lines.find((l) => l.length > 0);
  if (!first) return fallback;
  const counts: Record<string, number> = { ',': 0, ';': 0, '\t': 0 };
  for (const ch of first) {
    if (ch in counts) counts[ch]++;
  }
  let best = fallback;
  let bestN = 0;
  for (const d of [',', ';', '\t']) {
    if (counts[d] > bestN) {
      bestN = counts[d];
      best = d;
    }
  }
  return best;
}

/**
 * RFC-4180 state machine: quoted fields, `""` → `"`, embedded delimiter/newline inside
 * quotes, CRLF + LF. Strips a leading UTF-8 BOM. Returns ragged rows (no padding —
 * `createTable` pads to max width).
 */
export function parseCsv(text: string, delimiter: string): string[][] {
  const src = text.replace(/^\uFEFF/, '');
  if (src.length === 0) return [];
  const rows: string[][] = [];
  let field = '';
  let row: string[] = [];
  let inQuotes = false;
  let i = 0;

  while (i < src.length) {
    const ch = src[i];

    if (inQuotes) {
      if (ch === '"') {
        if (src[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
        continue;
      }
      field += ch;
      i++;
      continue;
    }

    // Not in quotes.
    if (ch === '"') {
      inQuotes = true;
      i++;
      continue;
    }
    if (ch === delimiter) {
      row.push(field);
      field = '';
      i++;
      continue;
    }
    if (ch === '\r') {
      // CRLF or lone CR → end of row.
      row.push(field);
      field = '';
      rows.push(row);
      row = [];
      i += src[i + 1] === '\n' ? 2 : 1;
      continue;
    }
    if (ch === '\n') {
      row.push(field);
      field = '';
      rows.push(row);
      row = [];
      i++;
      continue;
    }
    field += ch;
    i++;
  }

  // Flush a trailing field/row (file not newline-terminated) OR an empty final row from a
  // trailing newline: a real trailing newline already pushed its row above, leaving an
  // empty `row` + empty `field` — do not emit a spurious empty trailing row in that case.
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }

  return rows;
}

/**
 * Inverse of `parseCsv` for the matrix subset. Quote a cell iff it contains the delimiter,
 * `"`, `\n`, or `\r`; escape `"` → `""`; join cells by `delimiter`, rows by `\r\n`. Prepends
 * a UTF-8 BOM (Excel opens Cyrillic correctly).
 */
export function serializeTableCsv(model: { rows: string[][] }, delimiter = ','): string {
  const needsQuote = (cell: string): boolean =>
    cell.includes(delimiter) || cell.includes('"') || cell.includes('\n') || cell.includes('\r');

  const escapeCell = (cell: string): string =>
    needsQuote(cell) ? `"${cell.replace(/"/g, '""')}"` : cell;

  const body = model.rows
    .map((row) => row.map(escapeCell).join(delimiter))
    .join('\r\n');

  return BOM + body;
}
