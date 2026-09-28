/**
 * Unit tests for the table-block Yjs subtree model helpers (Order step 1–2).
 *
 * The model lives in `ydoc.getMap('tables')` as Y.Map<tableId, Y.Map{columns, rows}>.
 * These helpers are the ONLY sanctioned read/write path (Thin Client INVARIANT).
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import {
  getTablesMap,
  createTable,
  readTableModel,
  cloneTable,
  deleteTable,
  setCellText,
  setColumnWidth,
  addRow,
  addColumn,
  removeRow,
  removeColumn,
  tableAnchor,
  reconcileTables,
  serializeTables,
  applyTablesJson,
  modelToGfm,
  expandTableAnchorsToGfm,
  deleteTableFromDoc,
  deleteTableWithBackup,
  listDocumentTables,
  renameTable,
  insertTableAnchor,
  getModelLabel,
  normalizeTableLabel,
  createUnlinkedTable,
  TABLE_ANCHOR_RE,
  DEFAULT_COLUMN_WIDTH,
} from './table-block-model';
import { importGfmTables } from './gfm-table-import';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

describe('createTable / readTableModel', () => {
  it('creates a table from a cell-body matrix and reads it back', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    expect(typeof id).toBe('string');
    expect(readTableModel(doc, id)).toEqual({
      columns: [DEFAULT_COLUMN_WIDTH, DEFAULT_COLUMN_WIDTH],
      rows: [['a', 'b'], ['c', 'd']],
    });
  });

  it('registers the table under getTablesMap', () => {
    const id = createTable(doc, [['x']]);
    expect(getTablesMap(doc).has(id)).toBe(true);
  });

  it('returns null for an unknown id', () => {
    expect(readTableModel(doc, 'nope')).toBeNull();
  });

  it('cells are Y.Text (per-cell CRDT), not plain strings', () => {
    const id = createTable(doc, [['hi']]);
    const table = getTablesMap(doc).get(id)!;
    const rows = table.get('rows') as Y.Array<Y.Array<Y.Map<Y.Text>>>;
    const cell = rows.get(0).get(0);
    expect(cell.get('t')).toBeInstanceOf(Y.Text);
  });
});

describe('cloneTable (copy-paste of an anchor)', () => {
  it('clones content under a fresh id (no aliasing of one model)', () => {
    const src = createTable(doc, [['a', 'b']]);
    const dst = cloneTable(doc, src);
    expect(dst).not.toBe(src);
    expect(readTableModel(doc, dst)).toEqual(readTableModel(doc, src));
    // Mutating the clone must not touch the source (deep copy, not shared Y.Text).
    setCellText(doc, dst, 0, 0, 'changed');
    expect(readTableModel(doc, dst)!.rows[0][0]).toBe('changed');
    expect(readTableModel(doc, src)!.rows[0][0]).toBe('a');
  });
});

describe('cell + structure mutations', () => {
  it('setCellText replaces a cell body', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    setCellText(doc, id, 1, 0, 'X');
    expect(readTableModel(doc, id)!.rows[1][0]).toBe('X');
  });

  it('setColumnWidth writes the px width', () => {
    const id = createTable(doc, [['a', 'b']]);
    setColumnWidth(doc, id, 1, 240);
    expect(readTableModel(doc, id)!.columns[1]).toBe(240);
  });

  it('addRow appends an empty row of the right width', () => {
    const id = createTable(doc, [['a', 'b']]);
    addRow(doc, id);
    expect(readTableModel(doc, id)!.rows).toEqual([['a', 'b'], ['', '']]);
  });

  it('addColumn appends an empty cell to every row + a default width', () => {
    const id = createTable(doc, [['a'], ['b']]);
    addColumn(doc, id);
    const m = readTableModel(doc, id)!;
    expect(m.rows).toEqual([['a', ''], ['b', '']]);
    expect(m.columns).toEqual([DEFAULT_COLUMN_WIDTH, DEFAULT_COLUMN_WIDTH]);
  });

  it('removeRow drops a row', () => {
    const id = createTable(doc, [['a'], ['b'], ['c']]);
    removeRow(doc, id, 1);
    expect(readTableModel(doc, id)!.rows).toEqual([['a'], ['c']]);
  });

  it('removeColumn drops the cell + its width', () => {
    const id = createTable(doc, [['a', 'b', 'c']]);
    removeColumn(doc, id, 1);
    const m = readTableModel(doc, id)!;
    expect(m.rows).toEqual([['a', 'c']]);
    expect(m.columns.length).toBe(2);
  });
});

describe('tableAnchor', () => {
  it('builds the transclusion anchor', () => {
    expect(tableAnchor('My table', 'abc')).toBe('![My table](table:abc)');
  });
});

describe('reconcileTables (anchor ↔ model lifecycle)', () => {
  it('deletes a model whose anchor is gone (orphan model cleanup)', () => {
    const id = createTable(doc, [['a']]);
    // content has NO anchor for it → model is orphaned.
    reconcileTables(doc, 'some text without anchors');
    expect(getTablesMap(doc).has(id)).toBe(false);
  });

  it('keeps a model that still has its anchor', () => {
    const id = createTable(doc, [['a']]);
    reconcileTables(doc, `intro ${tableAnchor('t', id)} outro`);
    expect(getTablesMap(doc).has(id)).toBe(true);
  });

  it('clones the model for a DUPLICATED anchor and rewrites content to the new id', () => {
    const id = createTable(doc, [['a']]);
    const content = `${tableAnchor('t', id)}\n\n${tableAnchor('t', id)}`;
    const { content: rewritten, changed } = reconcileTables(doc, content);
    expect(changed).toBe(true);
    // Two distinct ids now exist (one original + one clone).
    expect(getTablesMap(doc).size).toBe(2);
    // The rewritten content references two different ids.
    const ids = [...rewritten.matchAll(/\(table:([^)]+)\)/g)].map((m) => m[1]);
    expect(ids.length).toBe(2);
    expect(new Set(ids).size).toBe(2);
  });

  it('is a no-op (changed=false) when anchors and models already match 1:1', () => {
    const id = createTable(doc, [['a']]);
    const content = tableAnchor('t', id);
    const { changed } = reconcileTables(doc, content);
    expect(changed).toBe(false);
  });
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
  it('returns "{}" when there are no tables', () => {
    expect(serializeTables(new Y.Doc())).toBe('{}');
  });

  it('emits {columns, rows} per table — matches the backend shape', () => {
    // Mirror of the backend fixture in test_table_capture.py::test_capture_emits_columns_and_rows
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    setColumnWidth(doc, id, 0, 200);
    setColumnWidth(doc, id, 1, 320);
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
    setColumnWidth(doc, id, 0, 50);
    setColumnWidth(doc, id, 1, 90);
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

  it('pads a single-column table', () => {
    const id = createTable(doc, [['H'], ['x']]);
    expect(modelToGfm(readTableModel(doc, id)!)).toBe('| H |\n| --- |\n| x |');
  });

  it('uses rows[0] as the header and the rest as the body', () => {
    const id = createTable(doc, [['H1', 'H2'], ['r1a', 'r1b'], ['r2a', 'r2b']]);
    const gfm = modelToGfm(readTableModel(doc, id)!);
    expect(gfm.split('\n')).toEqual([
      '| H1 | H2 |',
      '| --- | --- |',
      '| r1a | r1b |',
      '| r2a | r2b |',
    ]);
  });

  it('escapes pipes and converts newlines (byte-inverse of unescapeCell)', () => {
    const id = createTable(doc, [['h1', 'h2'], ['a | b', 'line1\nline2']]);
    const gfm = modelToGfm(readTableModel(doc, id)!);
    expect(gfm).toContain('a \\| b');
    expect(gfm).toContain('line1<br>line2');
    // The escaped pipe must NOT split the cell: still 2 columns.
    expect(gfm.split('\n')[2].split(/(?<!\\)\|/)).toHaveLength(4); // '' + 2 cells + ''
  });

  it('returns "" for an empty model (no rows)', () => {
    expect(modelToGfm({ columns: [160], rows: [] })).toBe('');
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

  it('expands multiple distinct tables independently', () => {
    const id1 = createTable(doc, [['A'], ['1']]);
    const id2 = createTable(doc, [['X', 'Y'], ['2', '3']]);
    const md = `${tableAnchor('t1', id1)}\n${tableAnchor('t2', id2)}`;
    const expanded = expandTableAnchorsToGfm(doc, md);
    expect(expanded).toContain('| A |\n| --- |\n| 1 |');
    expect(expanded).toContain('| X | Y |\n| --- | --- |\n| 2 | 3 |');
    expect((expanded.match(/table:/g) || []).length).toBe(0);
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

  it('TABLE_ANCHOR_RE matches the anchor shape (group 1 = label, group 2 = id)', () => {
    const re = new RegExp(TABLE_ANCHOR_RE.source, TABLE_ANCHOR_RE.flags.replace('g', ''));
    const m = '![My label](table:abc-123)'.match(re);
    expect(m).not.toBeNull();
    expect(m![1]).toBe('My label');
    expect(m![2]).toBe('abc-123');
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

  it('a single separator-grammar header cell AMONG normal cells round-trips fine', () => {
    // The non-dash cell ("B") prevents the row from reading as a separator.
    const id = createTable(doc, [['---', 'B'], ['1', '2']]);
    const gfm = modelToGfm(readTableModel(doc, id)!);
    const doc2 = new Y.Doc();
    const { count, text } = importGfmTables(doc2, gfm, 't');
    expect(count).toBe(1);
    const newId = [...text.matchAll(TABLE_ANCHOR_RE)][0][2];
    expect(readTableModel(doc2, newId)!.rows).toEqual([['---', 'B'], ['1', '2']]);
  });
});

/**
 * deleteTableFromDoc — whole-table removal for the References-panel "table badge" delete.
 *
 * Edits the document `content` Y.Text (anchor range delete) AND drops the `tables` model
 * in a SINGLE transact (one atomic Yjs update). This is the panel/badge write path; the
 * inline widget's removeAnchor() (broken-state only) is the other. Precedent for editing
 * content Y.Text directly (not via a view): table-reconcile.ts.
 */
describe('deleteTableFromDoc (whole-table delete)', () => {
  function seedContent(text: string) {
    doc.getText('content').insert(0, text);
  }

  it('removes the anchor from content AND drops the model', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    seedContent(`intro\n\n${tableAnchor('My table', id)}\n\noutro`);
    deleteTableFromDoc(doc, id, 'My table');
    expect(doc.getText('content').toString()).toBe('intro\n\n\n\noutro');
    expect(getTablesMap(doc).has(id)).toBe(false);
  });

  it('still drops the model when the anchor is already gone (orphan-model cleanup)', () => {
    const id = createTable(doc, [['a']]);
    seedContent('no anchor here');
    deleteTableFromDoc(doc, id, 'My table');
    expect(doc.getText('content').toString()).toBe('no anchor here');
    expect(getTablesMap(doc).has(id)).toBe(false);
  });

  it('leaves a DIFFERENT table untouched (deletes only the targeted id)', () => {
    const keep = createTable(doc, [['k']]);
    const drop = createTable(doc, [['d']]);
    seedContent(`${tableAnchor('keep', keep)} and ${tableAnchor('drop', drop)}`);
    deleteTableFromDoc(doc, drop, 'drop');
    expect(getTablesMap(doc).has(keep)).toBe(true);
    expect(getTablesMap(doc).has(drop)).toBe(false);
    expect(doc.getText('content').toString()).toBe(`${tableAnchor('keep', keep)} and `);
  });

  it('is a no-op (no throw) for an id that has neither anchor nor model', () => {
    seedContent('plain text');
    expect(() => deleteTableFromDoc(doc, 'never-existed', 'x')).not.toThrow();
    expect(doc.getText('content').toString()).toBe('plain text');
  });
});

/**
 * deleteTableWithBackup — awaits a backup (checkpoint POST) BEFORE mutating the model,
 * so a recovery point always exists and captures the table while it is still present.
 * On backup failure the delete is aborted (No silent degradation).
 */
describe('deleteTableWithBackup (backup-before-delete ordering)', () => {
  function seedContent(text: string) {
    doc.getText('content').insert(0, text);
  }

  it('awaits the backup BEFORE the model is dropped (ordering, not a literal)', async () => {
    const id = createTable(doc, [['a']]);
    seedContent(tableAnchor('T', id));
    const order: string[] = [];
    const backup = () =>
      new Promise<void>((resolve) => {
        // Table must still exist while the backup runs (it captures the live state).
        order.push(`backup:present=${getTablesMap(doc).has(id)}`);
        setTimeout(() => { order.push('backup-resolved'); resolve(); }, 0);
      });

    await deleteTableWithBackup(doc, id, 'T', backup);

    expect(order).toEqual(['backup:present=true', 'backup-resolved']);
    expect(getTablesMap(doc).has(id)).toBe(false);
  });

  it('aborts the delete (model RETAINED) when the backup rejects', async () => {
    const id = createTable(doc, [['a']]);
    seedContent(tableAnchor('T', id));
    const backup = () => Promise.reject(new Error('checkpoint failed'));

    await expect(deleteTableWithBackup(doc, id, 'T', backup)).rejects.toThrow('checkpoint failed');
    // The model survives — a failed backup must never lose data.
    expect(getTablesMap(doc).has(id)).toBe(true);
    expect(doc.getText('content').toString()).toBe(tableAnchor('T', id));
  });
});

/**
 * listDocumentTables — the reactive list derivation (pure half of useDocumentTables).
 *
 * Walks `content` anchors in DOCUMENT order (LINKED, unlinked:false), then appends every
 * model with no surviving anchor (UNLINKED, unlinked:true) so intact data is never hidden.
 * Orphan anchors (no model) are excluded; a stray duplicate id yields only its first
 * occurrence (post-reconcile ids are unique — this mirrors that invariant defensively).
 */
describe('listDocumentTables (panel list derivation)', () => {
  function seedContent(text: string) {
    doc.getText('content').insert(0, text);
  }

  it('lists tables in document/anchor order with label + shape', () => {
    const t1 = createTable(doc, [['a', 'b'], ['c', 'd']]);
    const t2 = createTable(doc, [['x'], ['y'], ['z']]);
    seedContent(`before\n\n${tableAnchor('First', t1)}\n\nmid\n\n${tableAnchor('Second', t2)}\n\nafter`);
    expect(listDocumentTables(doc)).toEqual([
      { table_id: t1, label: 'First', rows: 2, cols: 2, unlinked: false },
      { table_id: t2, label: 'Second', rows: 3, cols: 1, unlinked: false },
    ]);
  });

  it('INCLUDES an orphan MODEL as unlinked, AFTER the linked tables', () => {
    const anchored = createTable(doc, [['a']]);
    const orphan = createTable(doc, [['x', 'y']]); // model present, no anchor → unlinked
    seedContent(tableAnchor('Anchored', anchored));
    expect(listDocumentTables(doc)).toEqual([
      { table_id: anchored, label: 'Anchored', rows: 1, cols: 1, unlinked: false },
      { table_id: orphan, label: '', rows: 1, cols: 2, unlinked: true },
    ]);
  });

  it('excludes an orphan ANCHOR (no matching model)', () => {
    const id = createTable(doc, [['a']]);
    seedContent(`${tableAnchor('real', id)} and ${tableAnchor('ghost', 'missing-model')}`);
    expect(listDocumentTables(doc)).toEqual([
      { table_id: id, label: 'real', rows: 1, cols: 1, unlinked: false },
    ]);
  });

  it('skips a duplicate id, keeping the first occurrence', () => {
    const id = createTable(doc, [['a']]);
    seedContent(`${tableAnchor('one', id)} then ${tableAnchor('two', id)}`);
    expect(listDocumentTables(doc)).toEqual([
      { table_id: id, label: 'one', rows: 1, cols: 1, unlinked: false },
    ]);
  });

  it('returns [] when there are no tables and no anchors', () => {
    seedContent('just text, nothing tabular');
    expect(listDocumentTables(doc)).toEqual([]);
  });

  it('lists a model with empty content as unlinked (data still reachable)', () => {
    const orphan = createTable(doc, [['a']]);
    expect(listDocumentTables(doc)).toEqual([
      { table_id: orphan, label: '', rows: 1, cols: 1, unlinked: true },
    ]);
  });

  it('shows a renamed UNLINKED table with its model label', () => {
    const orphan = createTable(doc, [['a']]);
    renameTable(doc, orphan, '', 'My orphan'); // no anchor → model label only
    expect(listDocumentTables(doc)).toEqual([
      { table_id: orphan, label: 'My orphan', rows: 1, cols: 1, unlinked: true },
    ]);
  });
});

/**
 * renameTable / insertTableAnchor for UNLINKED tables — an orphan (no anchor) can still be
 * named (model-label fallback) and re-inserted into the document.
 */
describe('unlinked table rename + re-insert', () => {
  it('renameTable stores a model label when there is no anchor', () => {
    const id = createTable(doc, [['a']]);
    renameTable(doc, id, '', 'Renamed');
    expect(getModelLabel(doc, id)).toBe('Renamed');
    expect(doc.getText('content').toString()).toBe(''); // no anchor written
  });

  it('renameTable normalizes the model label (no `]`/newline)', () => {
    const id = createTable(doc, [['a']]);
    renameTable(doc, id, '', 'a]b\nc'); // ']' stripped, '\n' → ' '
    expect(getModelLabel(doc, id)).toBe('ab c');
  });

  it('insertTableAnchor re-links an orphan (anchor added, table becomes linked)', () => {
    const id = createTable(doc, [['a']]);
    renameTable(doc, id, '', 'Back');
    insertTableAnchor(doc, id, getModelLabel(doc, id));
    expect(doc.getText('content').toString()).toContain(tableAnchor('Back', id));
    // Now linked: listed as unlinked:false with the anchor label.
    expect(listDocumentTables(doc)).toEqual([
      { table_id: id, label: 'Back', rows: 1, cols: 1, unlinked: false },
    ]);
  });

  it('renameTable still rewrites the anchor for a LINKED table', () => {
    const id = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('Old', id));
    renameTable(doc, id, 'Old', 'New');
    expect(doc.getText('content').toString()).toBe(tableAnchor('New', id));
    expect(getModelLabel(doc, id)).toBe('New');
  });
});

/**
 * normalizeTableLabel — sanitize a user-typed label so it cannot break the
 * `![label](table:id)` anchor grammar. `]` ends the alt-text (`[^\]]*` in
 * TABLE_ANCHOR_RE) and a newline would split the anchor across lines; both are
 * neutralized at the write path (renameTable) so a rename can never produce a
 * broken anchor. Trailing/leading whitespace is trimmed.
 */
describe('normalizeTableLabel', () => {
  it('strips every "]" (ends the alt-text grammar)', () => {
    expect(normalizeTableLabel('a]b]c')).toBe('abc');
  });

  it('replaces newlines with a single space (no anchor split across lines)', () => {
    expect(normalizeTableLabel('line1\nline2')).toBe('line1 line2');
    expect(normalizeTableLabel('a\n\nb')).toBe('a  b');
  });

  it('trims leading/trailing whitespace', () => {
    expect(normalizeTableLabel('  spaced  ')).toBe('spaced');
  });

  it('preserves spaces, unicode, "(" and "[" (those do not break the grammar)', () => {
    expect(normalizeTableLabel('Таблица (копия) [v2]')).toBe('Таблица (копия) [v2');
    // "(" and "[" survive; only "]" and "\n" are neutralized.
  });

  it('returns "" for a whitespace-only input', () => {
    expect(normalizeTableLabel('   \n  ')).toBe('');
  });
});

/**
 * renameTable — the References-panel "table badge" rename write path. In ONE
 * `doc.transact` it locates `tableAnchor(oldLabel, id)` in `content` and replaces
 * that range with `tableAnchor(normalizeTableLabel(newLabel), id)`. Same
 * direct-content-edit precedent as `deleteTableFromDoc`. Collab merges via Yjs.
 */
describe('renameTable (whole-table label rename)', () => {
  function seedContent(text: string) {
    doc.getText('content').insert(0, text);
  }

  it('replaces the anchor label in content (single transact)', () => {
    const id = createTable(doc, [['a', 'b'], ['c', 'd']]);
    seedContent(`intro\n\n${tableAnchor('Old name', id)}\n\noutro`);
    renameTable(doc, id, 'Old name', 'New name');
    expect(doc.getText('content').toString()).toBe(`intro\n\n${tableAnchor('New name', id)}\n\noutro`);
  });

  it('is a no-op (no throw) when the old anchor is absent (peer already moved/removed it)', () => {
    const id = createTable(doc, [['a']]);
    seedContent('no anchor here at all');
    expect(() => renameTable(doc, id, 'Old name', 'New name')).not.toThrow();
    expect(doc.getText('content').toString()).toBe('no anchor here at all');
    // the model is untouched (rename only edits content, never the model).
    expect(getTablesMap(doc).has(id)).toBe(true);
  });

  it('leaves a DIFFERENT table anchor untouched (renames only the targeted id)', () => {
    const keep = createTable(doc, [['k']]);
    const target = createTable(doc, [['t']]);
    seedContent(`${tableAnchor('Keep me', keep)} and ${tableAnchor('Target', target)}`);
    renameTable(doc, target, 'Target', 'Renamed');
    expect(doc.getText('content').toString()).toBe(
      `${tableAnchor('Keep me', keep)} and ${tableAnchor('Renamed', target)}`,
    );
  });

  it('applies normalizeTableLabel on commit (strips "]" and newlines)', () => {
    const id = createTable(doc, [['a']]);
    seedContent(tableAnchor('Plain', id));
    renameTable(doc, id, 'Plain', 'a]b\nc');
    // "]" stripped, "\n" → space → the resulting anchor still parses.
    expect(doc.getText('content').toString()).toBe(tableAnchor('ab c', id));
  });

  it('the resulting anchor re-parses via TABLE_ANCHOR_RE with the new label', () => {
    const id = createTable(doc, [['a']]);
    seedContent(tableAnchor('Old', id));
    renameTable(doc, id, 'Old', 'Fresh label');
    const content = doc.getText('content').toString();
    const m = [...content.matchAll(TABLE_ANCHOR_RE)];
    expect(m).toHaveLength(1);
    expect(m[0][1]).toBe('Fresh label');
    expect(m[0][2]).toBe(id);
  });

  it('does not touch the tables model (label lives only in content)', () => {
    const id = createTable(doc, [['a', 'b']]);
    seedContent(tableAnchor('Old', id));
    const before = readTableModel(doc, id);
    renameTable(doc, id, 'Old', 'New');
    expect(readTableModel(doc, id)).toEqual(before);
  });
});

describe('createUnlinkedTable', () => {
  it('creates a table model whose matrix matches the input', () => {
    const id = createUnlinkedTable(doc, [['h1', 'h2'], ['1', '2']], 'My file');
    expect(readTableModel(doc, id)).toEqual({
      columns: [expect.any(Number), expect.any(Number)],
      rows: [['h1', 'h2'], ['1', '2']],
    });
  });

  it('stores the label as the model-level fallback (readable via getModelLabel)', () => {
    const id = createUnlinkedTable(doc, [['a']], 'Sales 2024');
    expect(getModelLabel(doc, id)).toBe('Sales 2024');
  });

  it('normalizes the label so it can seed a valid anchor later', () => {
    // An unnormalized label (with a newline / ']') would break an anchor; the stored
    // label must be the normalized form.
    const id = createUnlinkedTable(doc, [['a']], 'bad\nlabel]here');
    const stored = getModelLabel(doc, id);
    expect(stored).toBe(normalizeTableLabel('bad\nlabel]here'));
    expect(stored).not.toMatch(/[\n\]]/);
  });

  it('writes NO anchor into the document content (the table is unlinked)', () => {
    const id = createUnlinkedTable(doc, [['a', 'b']], 'No anchor');
    expect(doc.getText('content').toString()).toBe('');
    expect([...doc.getText('content').toString().matchAll(TABLE_ANCHOR_RE)]).toHaveLength(0);
    // model is registered under its id
    expect(getTablesMap(doc).has(id)).toBe(true);
  });
});
