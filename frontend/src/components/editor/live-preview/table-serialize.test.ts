/**
 * Unit tests for table serialization — checkpoint JSON and GFM projections.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import {
  getTablesMap,
  createTable,
  readTableModel,
  orderedColIds,
  setColumnWidth,
  orderedRowIds,
  setMerge,
} from './table-block-model';
import { DEFAULT_COLUMN_WIDTH } from './table-widths';
import { tableAnchor, deleteTableFromDoc, TABLE_ANCHOR_RE } from './table-document-ops';
import { serializeTables, applyTablesJson, modelToGfm, expandTableAnchorsToGfm } from './table-serialize';
import { importGfmTables } from './gfm-table-import';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

/**
 * serializeTables / applyTablesJson — full table-state capture for checkpoints.
 *
 * INVARIANT (shared shape, frontend ↔ backend): both producers emit the identical
 * JSON shape `{ columns: number[], rows: string[][] }` per table id. The fixture
 * below MUST stay byte-identical to the backend `test_table_capture.py` fixture so a
 * silent column-width drop fails loudly on either side.
 */
describe('serializeTables (checkpoint capture)', () => {
  // INVARIANT (cross-producer default column width): DEFAULT_COLUMN_WIDTH MUST equal
  // the backend table_serialize.DEFAULT_COLUMN_WIDTH (160). Why: a table restored from
  // a checkpoint with no captured widths renders at this default; a drift between the
  // two producers makes the widget render a different width than the editor authored.
  // Pinned here (and in test_table_capture.py) because the two producers cannot share
  // code — a change to either constant must update both.
  it('DEFAULT_COLUMN_WIDTH matches the backend constant (160)', () => {
    expect(DEFAULT_COLUMN_WIDTH).toBe(160);
  });
  it('emits {columns, rows} per table — matches the backend shape', () => {
    // Mirror of the backend fixture in test_table_capture.py::test_capture_emits_columns_and_rows
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    const cols = orderedColIds(doc, id)!;
    setColumnWidth(doc, id, cols[0], 200);
    setColumnWidth(doc, id, cols[1], 320);
    expect(JSON.parse(serializeTables(doc))).toEqual({
      [id]: { columns: [200, 320], rows: [['a', 'b'], ['c', 'd']] },
    });
  });

  it('preserves special chars / empty cells verbatim (lossless, unlike GFM export)', () => {
    const id = createTable(doc, [['a|b', ''], ['c\nd', '  e  ']]);
    const data = JSON.parse(serializeTables(doc));
    expect(data[id].rows).toEqual([['a|b', ''], ['c\nd', '  e  ']]);
  });
});

// TEST-WHY: cross-process-contract — mirror of test_table_capture.py::test_capture_restore_keeps_merges_and_omits_empty; both producers must emit `merges` after `rows`, only when non-empty, and restore must skip out-of-grid entries.
describe('serializeTables merges', () => {
  // TEST-WHY: cross-process-contract — mirror of the backend capture fixture; both producers emit `merges` after `rows`, only when non-empty.
  it('captures accepted areas, omits empty, restores and skips out-of-grid entries', () => {
    const id = createTable(doc, [['h1', 'h2', 'h3'], ['a', '', 'c'], ['', '', 'f']]);
    const plain = createTable(doc, [['x']]);
    const cols = orderedColIds(doc, id)!;
    [100, 120, 140].forEach((w, i) => setColumnWidth(doc, id, cols[i], w));
    const rows = orderedRowIds(doc, id)!;
    setMerge(doc, id, rows[1], cols[0], rows[2], cols[1]);
    const captured = JSON.parse(serializeTables(doc));
    expect(captured[id]).toEqual(
      { columns: [100, 120, 140], rows: [['h1', 'h2', 'h3'], ['a', '', 'c'], ['', '', 'f']], merges: [[1, 0, 2, 1]] },
    );
    expect(Object.keys(captured[plain])).toEqual(['columns', 'rows']);
    expect(Object.keys(captured[id])).toEqual(['columns', 'rows', 'merges']);
    const restored = new Y.Doc();
    applyTablesJson(restored, JSON.stringify({ ...captured, [id]: { ...captured[id], merges: [[1, 0, 2, 1], [5, 0, 6, 1]] } }));
    expect(JSON.parse(serializeTables(restored))).toEqual(captured);
  });
});

describe('applyTablesJson (restore)', () => {
  it('is a no-op for null (legacy restore leaves tables untouched)', () => {
    const id = createTable(doc, [['a']]);
    applyTablesJson(doc, null);
    expect(getTablesMap(doc).has(id)).toBe(true);
  });

  it('clears existing tables for "{}"', () => {
    createTable(doc, [['a']]);
    applyTablesJson(doc, '{}');
    expect(getTablesMap(doc).size).toBe(0);
  });

  it('rebuilds widths and cells from the captured JSON', () => {
    applyTablesJson(doc, JSON.stringify({
      t1: { columns: [200, 320], rows: [['a', 'b'], ['c', 'd']] },
    }));
    expect(readTableModel(doc, 't1')).toEqual({ columns: [200, 320], rows: [['a', 'b'], ['c', 'd']] });
  });

  it('replaces previous state (no leftover ids)', () => {
    const old = createTable(doc, [['z']]);
    applyTablesJson(doc, JSON.stringify({ fresh: { columns: [7], rows: [['q']] } }));
    expect(getTablesMap(doc).has(old)).toBe(false);
    expect(readTableModel(doc, 'fresh')!.rows).toEqual([['q']]);
  });

  it('round-trips through serialize → apply → serialize identically', () => {
    const id = createTable(doc, [['a|b', 'z\nw']]);
    const cols = orderedColIds(doc, id)!;
    setColumnWidth(doc, id, cols[0], 50);
    setColumnWidth(doc, id, cols[1], 90);
    const captured = serializeTables(doc);

    const doc2 = new Y.Doc();
    applyTablesJson(doc2, captured);
    expect(serializeTables(doc2)).toBe(captured);
  });
});

/**
 * modelToGfm / expandTableAnchorsToGfm — table model → portable GFM (clipboard copy path).
 *
 * modelToGfm is the byte-inverse of gfm-table-import.parseRow/unescapeCell: cell `|` → `\|`,
 * newline → `<br>`; rows[0] = header; a `| --- |` separator row follows; exactly one padding
 * space per side. expandTableAnchorsToGfm rewrites every `![label](table:id)` anchor in
 * arbitrary markdown to its GFM, leaving missing models untouched (never throws).
 */
describe('modelToGfm (model → GFM serializer)', () => {
  it('serializes a simple table with header + separator + body', () => {
    const id = createTable(doc, [['A', 'B'], ['1', '2']]);
    expect(modelToGfm(readTableModel(doc, id)!)).toBe(
      '| A | B |\n| --- | --- |\n| 1 | 2 |',
    );
  });

  it('escapes pipes and converts newlines (byte-inverse of unescapeCell)', () => {
    const id = createTable(doc, [['h1', 'h2'], ['a | b', 'line1\nline2']]);
    const gfm = modelToGfm(readTableModel(doc, id)!);
    expect(gfm).toContain('a \\| b');
    expect(gfm).toContain('line1<br>line2');
    // The escaped pipe must NOT split the cell: still 2 columns.
    expect(gfm.split('\n')[2].split(/(?<!\\)\|/)).toHaveLength(4); // '' + 2 cells + ''
  });

  it('round-trips cell text losslessly: readTableModel(import(modelToGfm(model)))', () => {
    // Header cells deliberately do NOT match the GFM separator grammar (see limitation test).
    const id = createTable(doc, [['h1', 'h2'], ['a|b', 'x\ny'], ['c', 'd']]);
    const model = readTableModel(doc, id)!;

    const doc2 = new Y.Doc();
    const { text } = importGfmTables(doc2, modelToGfm(model), 'Untitled');
    const newId = [...text.matchAll(TABLE_ANCHOR_RE)][0][2];
    // Cell text identity (column WIDTHS intentionally differ — importer uses default width).
    expect(readTableModel(doc2, newId)!.rows).toEqual(model.rows);
  });
});

describe('expandTableAnchorsToGfm (copy-path anchor expansion)', () => {
  it('replaces a table anchor with its GFM in arbitrary markdown', () => {
    const id = createTable(doc, [['A', 'B'], ['1', '2']]);
    const md = `intro text\n\n${tableAnchor('My table', id)}\n\noutro`;
    const expanded = expandTableAnchorsToGfm(doc, md);
    expect(expanded).toContain('| A | B |');
    expect(expanded).toContain('| --- | --- |');
    expect(expanded).toContain('intro text');
    expect(expanded).toContain('outro');
    expect(expanded).not.toContain('table:');
  });

  it('leaves a missing-model anchor untouched (never throws)', () => {
    const md = `text ${tableAnchor('orphan', 'does-not-exist')} end`;
    expect(expandTableAnchorsToGfm(doc, md)).toBe(md);
  });

  it('is read-only on the Y.Doc (no model added/removed)', () => {
    const id = createTable(doc, [['A'], ['1']]);
    const before = getTablesMap(doc).size;
    expandTableAnchorsToGfm(doc, tableAnchor('t', id));
    expect(getTablesMap(doc).size).toBe(before);
    expect(getTablesMap(doc).has(id)).toBe(true);
  });

});

describe('modelToGfm — GFM separator-grammar limitation', () => {
  // A header ROW whose EVERY cell body matches /^:?-+:?$/ (e.g. "---", ":--:") cannot
  // round-trip: GFM has no way to mark a row as data rather than separator, so the importer
  // (gfm-table-import.ts:isSeparatorRow) misclassifies the serialized header as the separator
  // and the table is not recognized. A SINGLE such cell among normal cells is fine — the row
  // only reads as a separator when ALL its cells are dash-like. Inherent to GFM, rare in
  // practice; body cells are unaffected.
  it('a header row of all-separator-grammar cells is NOT recoverable (documented limitation)', () => {
    const id = createTable(doc, [['---', ':--:'], ['1', '2']]);
    const gfm = modelToGfm(readTableModel(doc, id)!);
    const doc2 = new Y.Doc();
    const { count } = importGfmTables(doc2, gfm, 't');
    // The all-dash header is indistinguishable from a separator → no table recognized.
    expect(count).toBe(0);
  });

});

/**
 * deleteTableFromDoc — whole-table removal for the References-panel "table badge" delete.
 *
 * Edits the document `content` Y.Text (anchor range delete) AND drops the `tables` model
 * in a SINGLE transact (one atomic Yjs update). The one whole-table delete: the badge, the
 * cell menu and the widget's orphan-anchor "remove" button all go through it. Precedent for editing
 * content Y.Text directly (not via a view): table-reconcile.ts.
 */
