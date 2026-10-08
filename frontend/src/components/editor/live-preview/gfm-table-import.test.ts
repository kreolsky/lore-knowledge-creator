/**
 * Unit tests for GFM-markdown → table-object import (Session E).
 *
 * Mirrors the `parse_gfm_table` oracle contract (tests/backend/helpers.py). A pasted/imported
 * markdown GFM table becomes an editable table object: a `tables` model + an
 * `![label](table:id)` anchor spliced into the text in place of the GFM block.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import * as Y from 'yjs';
import { getTablesMap, readTableModel } from './table-block-model';
import { importGfmTables } from './gfm-table-import';

let doc: Y.Doc;
beforeEach(() => {
  doc = new Y.Doc();
});

const LABEL = 'Untitled';
const anchorRe = /^!\[Untitled\]\(table:([0-9a-f-]+)\)$/;

describe('importGfmTables', () => {
  it('converts a GFM table to a model + anchor', () => {
    const md = '| a | b |\n| --- | --- |\n| c | d |';
    const { text, count } = importGfmTables(doc, md, LABEL);
    expect(count).toBe(1);
    const m = text.match(anchorRe);
    expect(m).not.toBeNull();
    const id = m![1];
    expect(readTableModel(doc, id)).toEqual({
      columns: [expect.any(Number), expect.any(Number)],
      rows: [['a', 'b'], ['c', 'd']],
    });
  });

  it('leaves prose around the table intact', () => {
    const md = 'before\n\n| a | b |\n| --- | --- |\n| c | d |\n\nafter';
    const { text, count } = importGfmTables(doc, md, LABEL);
    expect(count).toBe(1);
    const lines = text.split('\n');
    expect(lines[0]).toBe('before');
    expect(lines[lines.length - 1]).toBe('after');
    expect(text).toMatch(/!\[Untitled\]\(table:[0-9a-f-]+\)/);
  });

  it('converts multiple tables independently', () => {
    const md = '| a |\n| --- |\n| x |\n\n| b |\n| --- |\n| y |';
    const { text, count } = importGfmTables(doc, md, LABEL);
    expect(count).toBe(2);
    expect(getTablesMap(doc).size).toBe(2);
    const ids = [...text.matchAll(/table:([0-9a-f-]+)/g)].map((m) => m[1]);
    expect(new Set(ids).size).toBe(2);
  });

  it('round-trips cell text losslessly (pipe, br, spaces)', () => {
    // escaped pipe -> literal, <br> -> newline, padding space stripped exactly once
    const md = '| h1 | h2 |\n| --- | --- |\n| a \\| b | line1<br>line2 |';
    const { text } = importGfmTables(doc, md, LABEL);
    const id = text.match(/table:([0-9a-f-]+)/)![1];
    expect(readTableModel(doc, id)!.rows[1]).toEqual(['a | b', 'line1\nline2']);
  });

  it('does not touch text without a GFM table', () => {
    const md = '# heading\n\nsome | inline pipe but no table\n';
    const { text, count } = importGfmTables(doc, md, LABEL);
    expect(count).toBe(0);
    expect(text).toBe(md);
    expect(getTablesMap(doc).size).toBe(0);
  });

  it('does NOT drop an all-empty data row (separator misclassification regression)', () => {
    // An all-empty row (`|   |   |`) is a data row, not the separator — it must survive
    // (the bug dropped it entirely). Assert the row COUNT and the surrounding rows rather
    // than exact empty-cell whitespace, which is a separate stripping concern.
    const md = '| a | b |\n| --- | --- |\n|   |   |\n| c | d |';
    const { text, count } = importGfmTables(doc, md, LABEL);
    expect(count).toBe(1);
    const id = text.match(/table:([0-9a-f-]+)/)![1];
    const rows = readTableModel(doc, id)!.rows;
    expect(rows.length).toBe(3); // header + empty row + last row — NOT 2
    expect(rows[0]).toEqual(['a', 'b']);
    expect(rows[2]).toEqual(['c', 'd']);
  });

  it('unescapes <br> variants case-insensitively (parity with backend parser)', () => {
    // Leading <BR/> → leading newline (one <br> before 'x').
    const md = '| h |\n| --- |\n| <BR/>x<br />y<br>z |';
    const { text } = importGfmTables(doc, md, LABEL);
    const id = text.match(/table:([0-9a-f-]+)/)![1];
    expect(readTableModel(doc, id)!.rows[1][0]).toBe('\nx\ny\nz');
  });
});
