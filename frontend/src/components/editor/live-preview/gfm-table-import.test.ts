/**
 * importGfmTables — the paste/drop GFM scanner, pinned to the backend twin.
 */
import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';
import { importGfmTables } from './gfm-table-import';
import { readTableModel } from './table-block-model';

// The fixture table: (case, markdown in, expected rewritten text, expected matrices in
// document order). `{TABLE}` stands for the one anchor `![Table](table:<id>)` the pass mints.
// BYTE-IDENTICAL BACKEND TWIN: tests/backend/test_table_serialize.py GFM_INGRESS_FIXTURE
// (the backend and frontend containers do not mount each other's trees, so the shared
// fixture cannot be one file). A case changed HERE changes THERE in the same step.
const GFM_INGRESS_FIXTURE: [string, string, string, string[][][]][] = [
  [
    'fenced-table-stays-text',
    '```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```',
    '```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```',
    [],
  ],
  [
    'no-outer-pipe-converts',
    'a | b\n--- | ---\n1 | 2',
    '{TABLE}',
    [[['a', 'b'], ['1', '2']]],
  ],
  [
    'setext-no-pipe-stays-heading',
    'a\n---',
    'a\n---',
    [],
  ],
  [
    'setext-with-pipe-stays-heading',
    'a | b\n---',
    'a | b\n---',
    [],
  ],
  [
    'missing-trailing-pipe-keeps-last-char',
    '| a | b\n| --- | --- |\n| 1 | 2',
    '{TABLE}',
    [[['a', 'b'], ['1', '2']]],
  ],
  [
    'image-embed-pipe-stays-one-cell',
    '| a | b |\n| --- | --- |\n| ![i|10x10](ref:x) | c |',
    '{TABLE}',
    [[['a', 'b'], ['![i|10x10](ref:x)', 'c']]],
  ],
  [
    'list-nested-table-converts',
    '- item\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |',
    '- item\n{TABLE}',
    [[['a', 'b'], ['1', '2']]],
  ],
];

const ANCHOR_RE = /!\[Table\]\(table:([^)]+)\)/g;

describe('importGfmTables (backend-twin fixture)', () => {
  // TEST-WHY: pure-logic-edge — the fixture pins the scanner's boundary rules and the byte agreement with the backend table_serialize scanner.
  it.each(GFM_INGRESS_FIXTURE)('%s', (_case, markdown, expectedText, expectedMatrices) => {
    const doc = new Y.Doc();
    const { text } = importGfmTables(doc, markdown, 'Table');
    expect(text.replace(ANCHOR_RE, '{TABLE}')).toBe(expectedText);
    const ids = [...text.matchAll(ANCHOR_RE)].map((m) => m[1]);
    expect(ids.map((id) => readTableModel(doc, id)?.rows)).toEqual(expectedMatrices);
  });
});
