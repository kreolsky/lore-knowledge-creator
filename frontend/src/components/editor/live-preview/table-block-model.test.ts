/**
 * Unit tests for the table-block Yjs subtree model helpers — schema v2 (id-keyed).
 *
 * The model lives in `ydoc.getMap('tables')` as
 * `Y.Map<tableId, Y.Map{v:2, columns, rows, cells}>` (see the model header). These
 * helpers are the ONLY sanctioned read/write path (Thin Client INVARIANT).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import {
  resolveMerges,
  setMerge,
  unmerge,
  cloneTable,
  getTablesMap,
  createTable,
  readTableModel,
  orderedColIds,
  orderedRowIds,
  addRow,
  addColumn,
  removeRow,
  removeColumn,
  moveRow,
  moveColumn,
  cellKey,
  getCellText,
  ensureCell,
  readColumnWidths,
  readTableShape,
  tableAwaitsMigration,
  TABLE_SCHEMA_VERSION,
  TABLE_LOCAL_ORIGIN,
  type RowId,
  type ColId,
  TABLE_SYSTEM_ORIGIN,
} from './table-block-model';
import { birthColumnWidths, DEFAULT_COLUMN_WIDTH } from './table-widths';
import { tableAnchor, cloneDuplicateAnchors } from './table-document-ops';
import { serializeTables, applyTablesJson } from './table-serialize';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

/** A minimal v1 table (positional Y.Arrays), hand-built as the pre-migration writer left it. */
function seedV1Table(doc: Y.Doc, id: string, matrix: string[][]): void {
  doc.transact(() => {
    const table = new Y.Map();
    const columns = new Y.Array();
    columns.push(matrix[0].map(() => {
      const col = new Y.Map();
      col.set('w', 100);
      return col;
    }));
    const rows = new Y.Array();
    rows.push(matrix.map((row) => {
      const yrow = new Y.Array();
      yrow.push(row.map((cell) => {
        const m = new Y.Map();
        m.set('t', new Y.Text(cell));
        return m;
      }));
      return yrow;
    }));
    table.set('columns', columns);
    table.set('rows', rows);
    getTablesMap(doc).set(id, table);
  });
}

function cellsOf(d: Y.Doc, id: string): Y.Map<Y.Text> {
  return getTablesMap(d).get(id)!.get('cells') as Y.Map<Y.Text>;
}

function rowsOf(d: Y.Doc, id: string): Y.Map<Y.Map<string | number>> {
  return getTablesMap(d).get(id)!.get('rows') as Y.Map<Y.Map<string | number>>;
}

/** Type into a cell the way the mount path does: ensureCell, then edit its Y.Text. */
function write(id: string, rowId: RowId, colId: ColId, text: string): void {
  const body = ensureCell(doc, id, rowId, colId)!;
  doc.transact(() => { body.delete(0, body.length); body.insert(0, text); }, TABLE_LOCAL_ORIGIN);
}

describe('createTable / readTableModel', () => {
  it('creates a table from a cell-body matrix and reads it back', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    expect(typeof id).toBe('string');
    // Born as schema v2 (the backend twin TABLE_SCHEMA_VERSION is 2 — a server
    // speaking another version is kept out by the ?proto gate, not by this constant).
    expect(TABLE_SCHEMA_VERSION).toBe(2);
    expect(getTablesMap(doc).get(id)!.get('v')).toBe(TABLE_SCHEMA_VERSION);
    // Born fixed, proportional to text length — see birthColumnWidths.
    expect(readTableModel(doc, id)).toEqual({
      columns: [422, 422],
      rows: [['a', 'b'], ['c', 'd']],
    });
  });

});

describe('cell addressing by ids (v2 contract)', () => {
  // TEST-WHY: recurring-regression — the most fix-churned model in the tree; id addressing, the missing-cell race and the in-place edit each regressed here in v1.
  it('addresses a cell ONLY by (tableId, rowId, colId): read/write, race, in-place', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    const [r0, r1] = orderedRowIds(doc, id)!;
    const [c0, c1] = orderedColIds(doc, id)!;

    // Id-addressed read/write round-trip.
    write(id, r1, c0, 'X');
    expect(readTableModel(doc, id)!.rows).toEqual([['a', 'b'], ['X', 'd']]);
    expect(getCellText(doc, id, r1, c0)!.toString()).toBe('X');
    expect(getCellText(doc, id, r0, c1)!.toString()).toBe('b');

    // Missing table/row/column: null, never a throw.
    expect(ensureCell(doc, id, 'no-row' as never, c0)).toBeNull();
    expect(ensureCell(doc, id, r0, 'no-col' as never)).toBeNull();
    expect(getCellText(doc, 'no-table', r0, c0)).toBeNull();

    // The concurrent add-row × add-column intersection: a missing cell of a LIVE row ×
    // column reads empty and is created by the first write (ensureCell = mount path).
    const key = cellKey(r0, c0);
    cellsOf(doc, id).delete(key);
    expect(readTableModel(doc, id)!.rows[0][0]).toBe('');
    expect(getCellText(doc, id, r0, c0)).toBeNull();
    expect(ensureCell(doc, id, r0, c0)).toBeInstanceOf(Y.Text);
    write(id, r0, c0, 'late');
    expect(readTableModel(doc, id)!.rows[0][0]).toBe('late');

    // An orphan cell (its row or column is gone) is ignored by every reader.
    cellsOf(doc, id).set('ghost-row:ghost-col', new Y.Text('stale'));
    expect(readTableModel(doc, id)!.rows).toEqual([['late', 'b'], ['X', 'd']]);

  });
});

describe('structure mutations (id-keyed)', () => {
  // TEST-WHY: pure-logic-edge — eager cells, one-pos moves, observer-invalidated order and origins are pure model logic; zero seams.
  it('structural ops, order and origins follow the v2 contract', () => {
    const origins: unknown[] = [];
    const id = createTable(doc, [['r0', 'c0'], ['r1', 'c1'], ['r2', 'c2']]);
    doc.on('afterTransaction', (tr) => origins.push(tr.origin));
    const rows = orderedRowIds(doc, id)!;
    const cols = orderedColIds(doc, id)!;

    // addRow appends an EAGER row (a cell per live column) under a fresh RowId.
    const rid = addRow(doc, id)!;
    expect(orderedRowIds(doc, id)!.at(-1)).toBe(rid);
    for (const cid of cols) expect(cellsOf(doc, id).get(cellKey(rid, cid))).toBeInstanceOf(Y.Text);
    expect(readTableModel(doc, id)!.rows.at(-1)).toEqual(['', '']);

    // addRow inserts between live anchors: below r0, above r1.
    const mid = addRow(doc, id, rows[0], rows[1])!;
    expect(orderedRowIds(doc, id)).toEqual([rows[0], mid, rows[1], rows[2], rid]);

    // moveRow / moveColumn are ONE pos write; the matrix read follows both axes.
    moveRow(doc, id, rows[2], null, rows[0]);
    expect(orderedRowIds(doc, id)!.slice(0, 2)).toEqual([rows[2], rows[0]]);
    moveColumn(doc, id, cols[1], null, cols[0]);
    expect(orderedColIds(doc, id)).toEqual([cols[1], cols[0]]);
    expect(readTableModel(doc, id)!.rows[0]).toEqual(['c2', 'r2']);

    // removeRow / removeColumn drop the axis entry AND its cells (one transaction).
    removeRow(doc, id, rid);
    expect(orderedRowIds(doc, id)).not.toContain(rid);
    removeColumn(doc, id, cols[0]);
    expect(orderedColIds(doc, id)).toEqual([cols[1]]);
    for (const r of orderedRowIds(doc, id)!) {
      expect(cellsOf(doc, id).has(cellKey(r, cols[0]))).toBe(false);
    }

    // Missing table/axis ids are no-ops, never throws.
    expect(addRow(doc, 'missing')).toBeNull();
    expect(addColumn(doc, 'missing', DEFAULT_COLUMN_WIDTH)).toBeNull();
    expect(() => removeRow(doc, id, 'ghost' as never)).not.toThrow();
    expect(() => moveRow(doc, id, 'ghost' as never)).not.toThrow();

    // Order: equal pos values (two replicas minted the same key) fall back to id —
    // a total order. Two rows forced onto one pos still sort unambiguously.
    const axis = rowsOf(doc, id);
    const live = orderedRowIds(doc, id)!;
    doc.transact(() => {
      axis.get(live[1])!.set('pos', String(axis.get(live[0])!.get('pos')));
    }, TABLE_LOCAL_ORIGIN);
    expect(orderedRowIds(doc, id)).toEqual([...[live[0], live[1]].sort(), ...live.slice(2)]);

    // Cache: a cell edit does NOT invalidate the cached order (same array reference);
    // a structural op rebuilds it.
    const cached = orderedRowIds(doc, id)!;
    write(id, live[0], orderedColIds(doc, id)![0], 'typing');
    expect(orderedRowIds(doc, id)).toBe(cached);
    addRow(doc, id);
    expect(orderedRowIds(doc, id)).not.toBe(cached);

    // Origins: user ops carry TABLE_LOCAL_ORIGIN (one undoable step each) — captured
    // by the listener registered after createTable; every op above is a user op.
    expect(origins.every((o) => o === TABLE_LOCAL_ORIGIN)).toBe(true);

    // Seeding (applyTablesJson) and reconciliation (cloneDuplicateAnchors) carry
    // TABLE_SYSTEM_ORIGIN — outside the undo stack (a replayed capture must not
    // "un-insert" on Ctrl+Z).
    const systemOrigins: unknown[] = [];
    doc.on('afterTransaction', (tr) => systemOrigins.push(tr.origin));
    applyTablesJson(doc, serializeTables(doc)); // rebuild under the same id
    const dup = cloneDuplicateAnchors(doc, `${tableAnchor('L', id)}${tableAnchor('L2', id)}`);
    expect(dup.changed).toBe(true);
    expect(systemOrigins.length).toBeGreaterThan(0);
    expect(systemOrigins.every((o) => o === TABLE_SYSTEM_ORIGIN)).toBe(true);
  });

  it('addColumn appends an empty cell to every row + a px width in a born-fixed table', () => {
    const id = createTable(doc, [['a'], ['b']]);
    const last = orderedColIds(doc, id)!.at(-1)!;
    const cid = addColumn(doc, id, DEFAULT_COLUMN_WIDTH, last, null)!; // append at the end
    expect(orderedColIds(doc, id)!.at(-1)).toBe(cid);
    expect(readTableModel(doc, id)!.rows).toEqual([['a', ''], ['b', '']]);
    expect(readTableModel(doc, id)!.columns).toEqual([844, DEFAULT_COLUMN_WIDTH]);
  });
});

describe('v1 tables (awaiting migration) — never empty', () => {
  // TEST-WHY: data-loss — a v1 table projected as EMPTY reaches a checkpoint, and restoring that checkpoint rebuilds it empty.
  it('a v1 table never projects empty: probed, skipped by capture, kept by restore', () => {
    const id = createTable(doc, [['a', 'b']]);
    expect(tableAwaitsMigration(getTablesMap(doc).get(id))).toBe(false);

    seedV1Table(doc, 'v1', [['x', 'y'], ['z', 'w']]);
    const v1 = getTablesMap(doc).get('v1');
    expect(tableAwaitsMigration(v1)).toBe(true);
    // Reads: broken (null) for the matrix, but the panel still sees its shape.
    expect(readTableModel(doc, 'v1')).toBeNull();
    expect(readColumnWidths(doc, 'v1')).toBeNull();
    expect(readTableShape(doc, 'v1')).toEqual({ rows: 2, cols: 2 });

    // Capture OMITS it — never emits an empty table.
    expect(JSON.parse(serializeTables(doc))).toEqual({
      [id]: { columns: [422, 422], rows: [['a', 'b']] },
    });

    // Restore KEEPS an unnamed v1 table (the snapshot holds no truth about it) and
    // REPLACES a named one (the rebuild wins).
    applyTablesJson(doc, JSON.stringify({ fresh: { columns: [7], rows: [['q']] } }));
    expect(tableAwaitsMigration(getTablesMap(doc).get('v1'))).toBe(true);
    expect(readTableModel(doc, 'fresh')!.rows).toEqual([['q']]);
    applyTablesJson(doc, JSON.stringify({ v1: { columns: [7], rows: [['rebuilt']] } }));
    expect(readTableModel(doc, 'v1')!.rows).toEqual([['rebuilt']]);
  });
});

// TEST-WHY: persisted-shape — merges are stored by anchor/end ids and must stay valid across removals and clones; a wrong upkeep silently dissolves or mis-sizes an area in every peer's document.
describe('merged areas (id-keyed)', () => {
  // TEST-WHY: data-loss — merges are stored by anchor/end ids; a wrong upkeep on removal or clone silently dissolves or mis-sizes an area in every peer's document.
  it('setMerge empties covered cells, removals shrink/re-anchor, clone keeps, unmerge keeps anchor text', () => {
    const doc = new Y.Doc();
    const id = createTable(doc, [['h', 'h', 'h'], ['a', 'b', 'c'], ['d', 'e', 'f'], ['g', 'h', 'i']]);
    const r = orderedRowIds(doc, id)!;
    const c = orderedColIds(doc, id)!;
    const txt = (ri: number, ci: number) => getCellText(doc, id, orderedRowIds(doc, id)![ri], orderedColIds(doc, id)![ci])!.toString();
    const area = () => resolveMerges(doc, id).map((a) => [a.r0, a.c0, a.r1, a.c1]);

    setMerge(doc, id, r[1], c[1], r[2], c[2]);
    expect(area()).toEqual([[1, 1, 2, 2]]);
    expect([txt(1, 1), txt(1, 2), txt(2, 1), txt(2, 2)]).toEqual(['b', '', '', '']);

    const dup = cloneTable(doc, id);
    expect(resolveMerges(doc, dup).map((a) => [a.r0, a.c0, a.r1, a.c1])).toEqual([[1, 1, 2, 2]]);

    removeColumn(doc, id, c[1]); // the anchor column: anchor moves right, text carried
    expect(area()).toEqual([[1, 1, 2, 1]]);
    expect(txt(1, 1)).toBe('b');
    removeRow(doc, id, r[2]); // the end row: area shrinks to one cell ⇒ dropped
    expect(area()).toEqual([]);

    const r2 = orderedRowIds(doc, id)!;
    const c2 = orderedColIds(doc, id)!;
    setMerge(doc, id, r2[1], c2[0], r2[2], c2[1]);
    expect(area()).toEqual([[1, 0, 2, 1]]);
    unmerge(doc, id, r2[1], c2[0]);
    expect(area()).toEqual([]);
    expect(txt(1, 0)).toBe('a');
  });
});
