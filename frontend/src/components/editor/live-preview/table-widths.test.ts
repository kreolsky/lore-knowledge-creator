/**
 * Unit tests for table column width math (pure px functions).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import {
  createTable,
  readTableModel,
  orderedColIds,
  setColumnWidth,
  setColumnWidths,
  addColumn,
  readColumnWidths,
} from './table-block-model';
import { scaleColumnWidths, MIN_COLUMN_WIDTH, birthColumnWidths } from './table-widths';
import { TABLE_ANCHOR_RE } from './table-document-ops';
import { importGfmTables } from './gfm-table-import';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

describe('column widths are always fixed px', () => {
  // TEST-WHY: pure-logic-edge — a 0 width would render as an auto column that reflows by itself.
  it('addColumn takes the given width and no write path stores a width under the minimum', () => {
    const doc = new Y.Doc();
    const id = createTable(doc, [['a', 'b']]);
    const cols = orderedColIds(doc, id)!;
    setColumnWidths(doc, id, cols.map((cid, i) => ({ id: cid, w: [100, 200][i] })));
    addColumn(doc, id, 200, cols[1], null);
    addColumn(doc, id, 0, null, cols[0]);
    setColumnWidth(doc, id, cols[1], 0);
    expect(readTableModel(doc, id)?.columns).toEqual([MIN_COLUMN_WIDTH, 100, MIN_COLUMN_WIDTH, 200]);
    expect(readColumnWidths(doc, id)!.map((c) => c.w)).toEqual([MIN_COLUMN_WIDTH, 100, MIN_COLUMN_WIDTH, 200]);
  });

  // TEST-WHY: pure-logic-edge — the GFM import birth path writes the birth formula, not auto columns.
  it('GFM import creates birth-width columns', () => {
    const { text } = importGfmTables(doc, '| A | B |\n| --- | --- |\n| 1 | 2 |', 't');
    const id = [...text.matchAll(TABLE_ANCHOR_RE)][0][2];
    expect(readTableModel(doc, id)!.columns).toEqual([422, 422]);
  });
});

describe('scaleColumnWidths', () => {
  const sum = (w: number[]) => w.reduce((a, b) => a + b, 0);

  // TEST-WHY: pure-logic-edge — the sum is what keeps the table edge under the cursor, and rounding breaks it silently.
  it('hits the requested total exactly, in integers', () => {
    const out = scaleColumnWidths([233, 233, 234], 401);
    expect(sum(out)).toBe(401);
    expect(out.every(Number.isInteger)).toBe(true);
  });

  // TEST-WHY: pure-logic-edge — a pinned column must not stop the shrink; the others absorb the rest.
  it('pins a column at the floor while the others absorb the shrink', () => {
    const out = scaleColumnWidths([50, 200, 400], 200);
    expect(out[0]).toBe(MIN_COLUMN_WIDTH);
    expect(sum(out)).toBe(200);
    expect(Math.min(...out)).toBeGreaterThanOrEqual(MIN_COLUMN_WIDTH);
    expect(scaleColumnWidths([100, 300, 500], 10)).toEqual([40, 40, 40]);
  });
});

// BYTE-IDENTICAL BACKEND TWIN: tests/backend/test_table_serialize.py BIRTH_WIDTH_FIXTURE
// (the containers do not mount each other's trees). A case changed HERE changes THERE.
const BIRTH_WIDTH_FIXTURE: [string, string[][], number[]][] = [
  ['empty-2x2-equal', [['', ''], ['', '']], [422, 422]],
  ['narrow-hash-wide-text', [['#', 'Description'], ['1', 'a'.repeat(100)]], [59, 785]],
  ['names', [['Name', 'Role'], ['Aragorn', 'Ranger'], ['Legolas', 'Elf']], [454, 390]],
  ['multiline-longest-trimmed-line', [['a\nbbbbbb  ', 'cc']], [563, 281]],
  ['ragged-row', [['x', 'y', 'z'], ['only']], [338, 253, 253]],
  ['remainder-to-widest-first', [['a', 'b', 'c']], [282, 281, 281]],
  ['code-points-not-bytes', [['ёжик', 'ab']], [482, 362]],
  ['too-many-columns-floor-at-min', [Array(25).fill('x')], Array(25).fill(40)],
];

describe('birthColumnWidths', () => {
  // TEST-WHY: pure-logic-edge — pins the birth-width formula and its byte agreement with the backend twin.
  it.each(BIRTH_WIDTH_FIXTURE)('%s', (_case, matrix, expected) => {
    expect(birthColumnWidths(matrix)).toEqual(expected);
  });
});
