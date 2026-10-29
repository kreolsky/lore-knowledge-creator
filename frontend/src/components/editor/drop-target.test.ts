/**
 * drop-target's pure helpers: drop-position insertion (dropInsertSpec) and the
 * drawn-width suffix of an image dropped into a table cell (dropEmbedForView).
 */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { EditorState } from '@codemirror/state';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

import { dropInsertSpec } from './drop-target';

describe('dropInsertSpec — content lands at the drop position', () => {
  const text = (s: string) => EditorState.create({ doc: s }).doc;

  it('block content mid-line is wrapped onto its own line (\\n before AND after)', () => {
    expect(dropInsertSpec(text('hello world'), 6, '![t](table:x)', 'block'))
      .toEqual({ from: 6, insert: '\n![t](table:x)\n' });
  });

  it('block content at a line end gets \\n before only (current table-drop shape)', () => {
    expect(dropInsertSpec(text('hello world'), 11, 'X', 'block'))
      .toEqual({ from: 11, insert: '\nX' });
  });

  it('block content at a line start gets \\n after only', () => {
    expect(dropInsertSpec(text('ab\ncd'), 3, 'X', 'block'))
      .toEqual({ from: 3, insert: 'X\n' });
  });
});
