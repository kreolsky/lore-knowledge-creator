/**
 * Combined Find match list over host text + table cells — the pure core of
 * find-matches.ts (CombinedMatchIndex, collectAnchorSpans, dropAnchorCrossing,
 * scanCellMatches). Inputs are injected readers over literal fixtures; the
 * view-bound half (attachFindIndex: compartment listener, tables-map observer,
 * ydoc late-bind) is exercised by the plan's post-step-5 live drives.
 */
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { SearchQuery } from '@codemirror/search';
import {
  SEARCH_HIGHLIGHT_LIMIT,
  cellCurrentMatchField,
  collectAnchorSpans,
  collectMatches,
  currentCellMatchField,
  dropAnchorCrossing,
  groupCellMarks,
  scanCellMatches,
  setCellCurrentMatch,
  setCurrentCellMatch,
  CombinedMatchIndex,
  type CombinedIndexInputs,
} from './find-matches';
import { cellKey, type ColId, type RowId } from './live-preview/table-block-model';

// Fixture ids: row/column n of a literal matrix is `r<n>` / `c<n>` (the model mints
// real ids; the pure index only needs stable strings in display order).
const R = (n: number): RowId => `r${n}` as RowId;
const C = (n: number): ColId => `c${n}` as ColId;
const at = (id: string): number => Number(id.slice(1));
/** The cell-match shape for row `r`, column `c` of a table `cols` wide. */
const cellMatch = (tableId: string, r: number, c: number, cols: number, from: number, to: number) => (
  { kind: 'cell' as const, tableId, rowId: R(r), colId: C(c), ord: r * cols + c, from, to });

function fixture(opts: { hostText: string; tables: Record<string, string[][]> }) {
  const fx = {
    hostText: opts.hostText,
    tables: structuredClone(opts.tables),
    query: new SearchQuery({ search: 'needle' }),
  };
  const inputs: CombinedIndexInputs = {
    getQuery: () => fx.query,
    getHostResult: () => collectMatches(EditorState.create({ doc: fx.hostText }), fx.query),
    getHostText: () => fx.hostText,
    getCellText: (id, rowId, colId) => fx.tables[id]?.[at(rowId)]?.[at(colId)] ?? null,
    getTableAxes: (id) => {
      const rows = fx.tables[id];
      return rows
        ? { rowIds: rows.map((_, i) => R(i)), colIds: (rows[0] ?? []).map((_, i) => C(i)) }
        : null;
    },
  };
  return { fx, index: new CombinedMatchIndex(inputs) };
}

describe('find-matches combined list (host + table cells)', () => {
  // TEST-WHY: pure-logic-edge — cell offsets are UTF-16 units in the cell's own text (a surrogate pair advances by 2 — the space Y.Text and a mounted cell editor share), and a multi-line cell is scanned as its own doc with ^/$ anchoring per cell line.
  it('scans a cell as its own doc with UTF-16 offsets', () => {
    expect(scanCellMatches(new SearchQuery({ search: 'needle' }), '👍👍 needle'))
      .toEqual([{ from: 5, to: 11 }]);
    const multi = 'line1\nneedle line2\nneedle';
    expect(scanCellMatches(new SearchQuery({ search: 'needle' }), multi)).toEqual([
      { from: 6, to: 12 },
      { from: 19, to: 25 },
    ]);
    expect(scanCellMatches(new SearchQuery({ search: '^needle', regexp: true }), multi))
      .toEqual([{ from: 6, to: 12 }, { from: 19, to: 25 }]);
    expect(scanCellMatches(new SearchQuery({ search: 'line2$', regexp: true }), multi))
      .toEqual([{ from: 13, to: 18 }]);
  });

  // TEST-WHY: pure-logic-edge — the combined list interleaves host and cell matches in document order (cells at their anchor's position, row/col order), drops host matches crossing an anchor, ignores unanchored and duplicate-anchored tables, and caps the COMBINED list at 1000.
  it('orders host/cell/host, excludes anchor-crossing, unanchored and duplicate anchors, and caps the combined list', () => {
    // Anchor spans: first occurrence of a duplicated id only.
    const dup = '![T](table:t1) mid ![T](table:t1)';
    expect(collectAnchorSpans(dup)).toEqual([{ id: 't1', from: 0, to: 14 }]);
    // A host match overlapping an anchor (partly OR wholly) has no text to land in.
    expect(dropAnchorCrossing(
      [{ from: 0, to: 7 }, { from: 3, to: 12 }, { from: 18, to: 20 }],
      collectAnchorSpans('xx ![T](table:t1) yy'),
    )).toEqual([{ from: 18, to: 20 }]);

    // Document order: host before the anchor → the anchor's cells (row-major) → host after.
    const ordered = fixture({
      hostText: 'needle A\n\n![T](table:t1)\n\nneedle B',
      tables: { t1: [['needle', 'x'], ['y', 'needle']] },
    });
    expect(ordered.index.derive().matches).toEqual([
      { kind: 'host', from: 0, to: 6 },
      cellMatch('t1', 0, 0, 2, 0, 6),
      cellMatch('t1', 1, 1, 2, 0, 6),
      { kind: 'host', from: 26, to: 32 },
    ]);

    // Anchor-crossing host matches dropped, the same words inside cells kept;
    // every cell of the table sorts at the anchor, before the trailing host match.
    const crossing = fixture({
      hostText: 'xx ![T](table:t1) yy',
      tables: { t1: [['yy', 'table:t1']] },
    });
    crossing.fx.query = new SearchQuery({ search: 'xx !\\[T\\]|yy|table:t1', regexp: true });
    const crossingMatches = crossing.index.derive().matches;
    expect(crossingMatches).toEqual([
      cellMatch('t1', 0, 0, 2, 0, 2), // 'yy'
      cellMatch('t1', 0, 1, 2, 0, 8), // 'table:t1' — a CELL match, kept
      { kind: 'host', from: 18, to: 20 },                              // 'yy'
    ]);
    expect(crossingMatches.some(m => m.kind === 'host' && m.from < 17 && m.to > 3)).toBe(false);

    // An unanchored table is never enumerated — no match AND no cell of it scanned.
    const unanchored = fixture({
      hostText: '![T](table:t1)',
      tables: { t1: [['needle']], t2: [['needle', 'needle']] },
    });
    expect(unanchored.index.derive().matches.filter(m => m.kind === 'cell')).toEqual([
      cellMatch('t1', 0, 0, 1, 0, 6),
    ]);
    expect(unanchored.index.cellScanCount).toBe(1);

    // An invalid query (empty search) is empty without scanning any cell.
    const invalid = fixture({ hostText: 'needle ![T](table:t1)', tables: { t1: [['needle']] } });
    invalid.fx.query = new SearchQuery({ search: '' });
    expect(invalid.index.derive()).toEqual({ matches: [], truncated: false });
    expect(invalid.index.cellScanCount).toBe(0);

    // The cap is over the COMBINED list: 500 cells (at the leading anchor) + 500 host = 1000.
    const big = fixture({
      hostText: `![T](table:t1)\n${'needle '.repeat(600)}`,
      tables: { t1: Array.from({ length: 5 }, () => Array.from({ length: 100 }, () => 'needle')) },
    });
    const bigResult = big.index.derive();
    expect(bigResult.matches.length).toBe(SEARCH_HIGHLIGHT_LIMIT);
    expect(bigResult.truncated).toBe(true);
    expect(bigResult.matches[0].kind).toBe('cell');
    expect(bigResult.matches.filter(m => m.kind === 'cell').length).toBe(500);
    expect(bigResult.matches.filter(m => m.kind === 'host').length).toBe(500);
    // Host-side truncation (1001 host matches, no tables) flags the combined result too.
    const hostOnly = fixture({ hostText: 'needle '.repeat(1001), tables: {} });
    const hostOnlyResult = hostOnly.index.derive();
    expect(hostOnlyResult.matches.length).toBe(SEARCH_HIGHLIGHT_LIMIT);
    expect(hostOnlyResult.truncated).toBe(true);
  });

  // TEST-WHY: pure-logic-edge — the per-cell cache contract: a host edit moves anchors but never changes a cell (cache kept), while a cell/table/whole-subtree change and a query change each rescan exactly their scope.
  it('keeps the cell cache across host edits and invalidates by scope', () => {
    const { fx, index } = fixture({
      hostText: 'needle ![T](table:t1)',
      tables: { t1: [['needle', 'o'], ['o', 'o']] },
    });
    expect(index.derive().matches.length).toBe(2); // 1 host + 1 cell
    expect(index.cellScanCount).toBe(4);           // every cell of the anchored table, once

    // Host edit: anchors + host re-read, cell cache intact.
    fx.hostText = 'needle needle ![T](table:t1)';
    index.hostChanged();
    expect(index.derive().matches.length).toBe(3);
    expect(index.cellScanCount).toBe(4);

    // A clean re-derive returns the cached result object as-is.
    const d1 = index.derive();
    expect(index.derive()).toBe(d1);
    expect(index.cellScanCount).toBe(4);

    // One cell changed: exactly that cell rescans.
    fx.tables.t1[0][1] = 'needle';
    index.cellChanged('t1', cellKey(R(0), C(1)));
    expect(index.derive().matches.length).toBe(4);
    expect(index.cellScanCount).toBe(5);

    // Structural change (a row appeared): the whole table rescans.
    fx.tables.t1.push(['o', 'o']);
    index.tableChanged('t1');
    index.derive();
    expect(index.cellScanCount).toBe(11); // 5 + 6 cells

    // Whole subtree / doc rebind: everything goes.
    index.allTablesChanged();
    index.derive();
    expect(index.cellScanCount).toBe(17); // 11 + 6 cells

    // Query change: the cache is cleared even without an explicit invalidation.
    fx.query = new SearchQuery({ search: 'zzz' });
    const zzz = index.derive();
    expect(index.cellScanCount).toBe(23); // 17 + 6 cells
    expect(zzz.matches).toEqual([]);
  });

  // TEST-WHY: pure-logic-edge — the published per-table marks must use the widget's own cell key (cellKey(rowId, colId)) and preserve each cell's own-text offsets, so the relay hands a widget exactly the sets its cellHosts/cellViews are keyed by; host matches and match-less tables contribute no entry (the relay's empty-map default clears those).
  it('groups cell matches per table with the widget cell keys', () => {
    const grouped = groupCellMarks([
      { kind: 'host', from: 0, to: 6 },
      cellMatch('t1', 2, 1, 2, 0, 6),
      cellMatch('t1', 2, 1, 2, 10, 16),
      cellMatch('t1', 0, 0, 2, 4, 10),
      cellMatch('t2', 0, 0, 1, 4, 7),
    ]);
    expect([...grouped.keys()]).toEqual(['t1', 't2']);
    expect(grouped.get('t1')).toEqual(new Map([
      [cellKey(R(0), C(0)), [{ from: 4, to: 10 }]],
      [cellKey(R(2), C(1)), [{ from: 0, to: 6 }, { from: 10, to: 16 }]],
    ]));
    expect(grouped.get('t2')).toEqual(new Map([
      [cellKey(R(0), C(0)), [{ from: 4, to: 7 }]],
    ]));
    // A list with no cell match publishes no table entry — the relay's empty
    // default is what clears a table that lost its last match.
    expect(groupCellMarks([{ kind: 'host', from: 0, to: 6 }])).toEqual(new Map());
    expect(groupCellMarks([])).toEqual(new Map());
  });

  // TEST-WHY: pure-logic-edge — the CELL-side current range must survive non-consuming edits (map through local changes) and drop to null when consumed or zero-length (a zero-length mark paints a false current), while the HOST-side slot is a plain effect slot — its offsets index the CELL's text, so a host doc change must not remap them.
  it('tracks the current cell match: cell side maps through edits, host side is a plain slot', () => {
    // Doc-backed states: the cell-side field maps its range through changesets,
    // so its positions must live inside the doc (as a real cell's always do).
    const cell = EditorState.create({ doc: 'abcdefghij', extensions: [cellCurrentMatchField] });
    const set = cell.update({ effects: setCellCurrentMatch.of({ from: 3, to: 9 }) }).state;
    expect(set.field(cellCurrentMatchField)).toEqual({ from: 3, to: 9 });
    // An edit before the match shifts it with the text.
    expect(set.update({ changes: { from: 0, insert: 'ab' } }).state.field(cellCurrentMatchField))
      .toEqual({ from: 5, to: 11 });
    // An edit deleting the whole range clears it — no zero-length current mark.
    expect(set.update({ changes: { from: 3, to: 9, insert: '' } }).state.field(cellCurrentMatchField))
      .toBeNull();
    // A zero-length push is dropped, a null push clears.
    expect(cell.update({ effects: setCellCurrentMatch.of({ from: 2, to: 2 }) }).state.field(cellCurrentMatchField))
      .toBeNull();
    expect(set.update({ effects: setCellCurrentMatch.of(null) }).state.field(cellCurrentMatchField))
      .toBeNull();

    const host = EditorState.create({ doc: 'host text', extensions: [currentCellMatchField] });
    const pointed = host.update({
      effects: setCurrentCellMatch.of({ tableId: 't1', rowId: R(1), colId: C(0), from: 3, to: 9 }),
    }).state;
    expect(pointed.field(currentCellMatchField)).toEqual({ tableId: 't1', rowId: R(1), colId: C(0), from: 3, to: 9 });
    // Host doc changes never remap the slot — its offsets index the CELL's Y.Text.
    expect(pointed.update({ changes: { from: 0, insert: 'ab' } }).state.field(currentCellMatchField))
      .toEqual({ tableId: 't1', rowId: R(1), colId: C(0), from: 3, to: 9 });
    expect(pointed.update({ effects: setCurrentCellMatch.of(null) }).state.field(currentCellMatchField))
      .toBeNull();
  });
});
