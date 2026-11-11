/**
 * Unit tests for table document ops — anchor/label edits and whole-table delete paths.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import { getTablesMap, createTable, readTableModel, getModelLabel } from './table-block-model';
import {
  tableAnchor,
  deleteTableFromDoc,
  deleteTableWithBackup,
  listDocumentTables,
  renameTable,
  insertTableAnchor,
  normalizeTableLabel,
  createUnlinkedTable,
  TABLE_ANCHOR_RE,
} from './table-document-ops';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

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

  it('lists a model with empty content as unlinked (data still reachable)', () => {
    // The CSV/TSV panel-import path builds the model AND stores its label atomically
    // (createUnlinkedTable) — the badge shows that label immediately, no anchor needed.
    const orphan = createUnlinkedTable(doc, [['a', 'b']], 'CSV] data');
    expect(getModelLabel(doc, orphan)).toBe('CSV data');
    expect(readTableModel(doc, orphan)!.rows).toEqual([['a', 'b']]);
    expect(listDocumentTables(doc)).toEqual([
      { table_id: orphan, label: 'CSV data', rows: 1, cols: 2, unlinked: true },
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

  it('applies normalizeTableLabel on commit (strips "]" and newlines)', () => {
    const id = createTable(doc, [['a']]);
    seedContent(tableAnchor('Plain', id));
    renameTable(doc, id, 'Plain', 'a]b\nc');
    // "]" stripped, "\n" → space → the resulting anchor still parses.
    expect(doc.getText('content').toString()).toBe(tableAnchor('ab c', id));
  });

});
