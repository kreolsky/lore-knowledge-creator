/** Table column width math — pure functions over px numbers, no Yjs (see SYSTEM: table-block, table-block-model.ts). */

/** Narrowest column width in px — the floor of every width write (see INVARIANT on
 * `fixedWidth`). New tables are born by `birthColumnWidths`, a new column inherits a
 * neighbour's width, legacy `0` widths are frozen by `freezeAutoWidths`. */
export const MIN_COLUMN_WIDTH = 40;

/**
 * Default FIXED column width in px — the restore fallback for captured tables
 * with missing widths (mirrored in `backend/table_serialize.py`; the two
 * constants must stay equal — see table-serialize.test.ts).
 */
export const DEFAULT_COLUMN_WIDTH = 160;

/**
 * WHY: the px width a new table is born at = the DEFAULT text column width
 * (`--content-max-width` in index.css, 52.75rem at 16px). A viewer's wider column
 * preset does not change it: the width is stored and shared by every viewer. MUST equal the backend
 * `BIRTH_TABLE_WIDTH` in `backend/table_serialize.py`.
 */
export const BIRTH_TABLE_WIDTH = 844;
/**
 * WHY: an EMPTY table from the insert command is born at 60% of `BIRTH_TABLE_WIDTH`, so the
 * free space beside it shows room for more columns (operator ruling). Frontend-only: the
 * server never creates an empty table, so the backend twin has no counterpart.
 */
export const INSERT_TABLE_WIDTH = Math.round(BIRTH_TABLE_WIDTH * 0.6);
const BIRTH_WEIGHT_MIN = 3;
const BIRTH_WEIGHT_MAX = 40;

/** Fixed px widths for a table at birth, proportional to cell text length.
 * Weight = code points of the longest trimmed line per cell, clamped to [3, 40].
 * INVARIANT(persisted): TEXT LENGTH only, never font metrics; equals backend `birth_column_widths`.
 * Why: server and every client must write the same numbers into the shared model;
 * after birth widths change only by a user's resize.
 */
export function birthColumnWidths(matrix: string[][], total = BIRTH_TABLE_WIDTH): number[] {
  const width = Math.max(matrix.reduce((m, r) => Math.max(m, r.length), 1), 1);
  const weights = Array.from({ length: width }, (_, col) => {
    let longest = 0;
    for (const row of matrix) {
      if (col >= row.length) continue;
      for (const line of String(row[col]).split('\n')) longest = Math.max(longest, [...line.trim()].length);
    }
    return Math.min(Math.max(longest, BIRTH_WEIGHT_MIN), BIRTH_WEIGHT_MAX);
  });
  const weightSum = weights.reduce((a, b) => a + b, 0);
  const px = weights.map((w) => Math.max(Math.round((total * w) / weightSum), MIN_COLUMN_WIDTH));
  const widest = px.indexOf(Math.max(...px));
  px[widest] = Math.max(px[widest] + total - px.reduce((a, b) => a + b, 0), MIN_COLUMN_WIDTH);
  return px;
}

/**
 * Replace every legacy `0` ("auto") width with a px width by the birth rule. No zero ⇒
 * unchanged; every column zero ⇒ `birthColumnWidths` of the table; mixed ⇒ px columns keep
 * theirs and the zero columns share `max(BIRTH_TABLE_WIDTH − sum(px), zeroCount × MIN)` by
 * the birth rule over their own cells. Equals backend `freeze_auto_widths` (vectors:
 * `fixtures/table-v2.json`, key `freeze_auto_widths`).
 */
export function freezeAutoWidths(widths: number[], matrix: string[][]): number[] {
  const zeroIdx = widths.flatMap((w, i) => (Number(w) === 0 ? [i] : []));
  if (!zeroIdx.length) return widths.map(Number);
  const rows = matrix.length ? matrix : [[]];
  const sub = rows.map((r) => zeroIdx.map((i) => (i < r.length ? String(r[i]) : '')));
  const pxSum = widths.reduce((sum, w) => sum + Number(w), 0);
  const total = Math.max(BIRTH_TABLE_WIDTH - pxSum, zeroIdx.length * MIN_COLUMN_WIDTH);
  const born = birthColumnWidths(sub, total);
  let k = 0;
  return widths.map((w) => (Number(w) === 0 ? born[k++] : Number(w)));
}

/** WHY: the content fit caps one column at this share, so one long paragraph cannot
 * squeeze every other column to the minimum (operator ruling). */
export const FIT_COLUMN_MAX_SHARE = 0.6;

/**
 * Content-proportional px widths summing to `total` (the birth rule), with no column past
 * `FIT_COLUMN_MAX_SHARE` of the table when there is more than one: a capped column's excess
 * is spread over the rest in proportion to their widths.
 */
export function fitColumnWidths(matrix: string[][], total: number): number[] {
  const px = birthColumnWidths(matrix, total);
  if (px.length < 2) return px;
  const cap = Math.floor(total * FIT_COLUMN_MAX_SHARE);
  const capped = new Set<number>();
  for (let over = px.findIndex((w) => w > cap); over !== -1; over = px.findIndex((w, i) => w > cap && !capped.has(i))) {
    capped.add(over);
    px[over] = cap;
    const rest = px.flatMap((w, i) => (capped.has(i) ? [] : [w]));
    const scaled = scaleColumnWidths(rest, total - capped.size * cap);
    let k = 0;
    px.forEach((_, i) => { if (!capped.has(i)) px[i] = scaled[k++]; });
  }
  return px;
}

/**
 * Scale widths proportionally to `total`. A column reaching `MIN_COLUMN_WIDTH` pins there
 * and the others absorb the rest, so the smallest column does not stop a shrink. Integers
 * summing exactly to `max(total, n * MIN)`: the rounding remainder goes to the widest
 * column, which keeps the table edge under the cursor.
 */
export function scaleColumnWidths(widths: number[], total: number): number[] {
  const target = Math.max(Math.round(total), widths.length * MIN_COLUMN_WIDTH);
  const pinned = new Set<number>();
  let scale = 0;
  for (let settled = false; !settled;) {
    const free = widths.filter((_, i) => !pinned.has(i));
    const freeSum = free.reduce((sum, w) => sum + w, 0);
    scale = freeSum > 0 ? (target - pinned.size * MIN_COLUMN_WIDTH) / freeSum : 0;
    const below = widths.flatMap((w, i) => (!pinned.has(i) && w * scale < MIN_COLUMN_WIDTH ? [i] : []));
    below.forEach((i) => pinned.add(i));
    settled = below.length === 0;
  }
  const scaled = widths.map((w, i) => (pinned.has(i) ? MIN_COLUMN_WIDTH : Math.round(w * scale)));
  const widest = scaled.indexOf(Math.max(...scaled));
  scaled[widest] += target - scaled.reduce((sum, w) => sum + w, 0);
  return scaled;
}
