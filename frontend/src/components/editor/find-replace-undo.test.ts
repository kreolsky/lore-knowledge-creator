/** Replace-all over a real Y.Doc: one step on the document-wide undo stack, stale once it is not the top. */

import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';
import { SearchQuery } from '@codemirror/search';
import {
  createTable,
  getCellText,
  orderedColIds,
  orderedRowIds,
  TABLE_LOCAL_ORIGIN,
} from './live-preview/table-block-model';
import { tableAnchor } from './live-preview/table-document-ops';
import { applyReplaceAll, planReplaceAll, REPLACE_ALL_ORIGIN } from './find-replace-undo';
import { createDocumentUndo } from '../../collab/document-undo';

function seed(): { doc: Y.Doc; host: Y.Text; id: string; manager: Y.UndoManager } {
  const doc = new Y.Doc();
  const id = createTable(doc, [['foo bar', 'x'], ['a|foo', 'foo\nfoo']]);
  const host = doc.getText('content');
  host.insert(0, `foo before ${tableAnchor('foo', id)} foo after`);
  // Created after seeding, like the provider at join: the seed is not an undo step.
  const manager = createDocumentUndo(doc, [TABLE_LOCAL_ORIGIN, REPLACE_ALL_ORIGIN]);
  return { doc, host, id, manager };
}

/** Cell text at display position (r, c) — the test reads through the model's order. */
const text = (doc: Y.Doc, id: string, r: number, c: number): Y.Text =>
  getCellText(doc, id, orderedRowIds(doc, id)![r], orderedColIds(doc, id)![c])!;
const cell = (doc: Y.Doc, id: string, r: number, c: number): string => text(doc, id, r, c).toString();

describe('replace all', () => {
  // TEST-WHY: pure-logic-edge — one Replace-all writes host + every cell of an anchored table (anchor label untouched, `|` and multi-line cells included) as ONE step of the shared stack, and the panel Undo reverts all of it together.
  it('replaces host and cells and undoes them in one step', () => {
    const { doc, host, id, manager } = seed();
    const plan = planReplaceAll(new SearchQuery({ search: 'foo', replace: 'baz' }), host.toString(), doc);
    expect(plan.host).toHaveLength(2);
    expect(plan.cellCount).toBe(4);
    const undo = applyReplaceAll(doc, host, plan)!;
    expect(manager.undoStack).toHaveLength(1);
    expect(host.toString()).toBe(`baz before ${tableAnchor('foo', id)} baz after`);
    expect([cell(doc, id, 0, 0), cell(doc, id, 1, 0), cell(doc, id, 1, 1)]).toEqual(['baz bar', 'a|baz', 'baz\nbaz']);
    expect(undo.undo()).toBe(true);
    expect(host.toString()).toBe(`foo before ${tableAnchor('foo', id)} foo after`);
    expect([cell(doc, id, 0, 0), cell(doc, id, 1, 0), cell(doc, id, 1, 1)]).toEqual(['foo bar', 'a|foo', 'foo\nfoo']);
    expect(undo.undo()).toBe(false);
  });

  // TEST-WHY: pure-logic-edge — the INVARIANT(corruption) at applyReplaceAll: a tracked edit after the Replace-all makes the panel Undo stale (onStale fires, undo() pops nothing, the later edit survives); a Ctrl+Z that already reverted it stales it too; regexp `$1` groups substitute like the stock replace.
  it('keeps later edits of another origin through undo', () => {
    const { doc, host, id, manager } = seed();
    let stale = 0;
    const undo = applyReplaceAll(doc, host,
      planReplaceAll(new SearchQuery({ search: '(f)oo', regexp: true, replace: '$1ee' }), host.toString(), doc),
      () => { stale++; })!;
    expect(cell(doc, id, 0, 0)).toBe('fee bar');
    doc.transact(() => text(doc, id, 0, 1).insert(1, 'y'), TABLE_LOCAL_ORIGIN);
    expect(stale).toBe(1);
    expect(undo.undo()).toBe(false);
    expect(cell(doc, id, 0, 1)).toBe('xy');
    expect(cell(doc, id, 0, 0)).toBe('fee bar');
    const again = applyReplaceAll(doc, host,
      planReplaceAll(new SearchQuery({ search: 'fee', replace: 'foo' }), host.toString(), doc), () => { stale++; })!;
    manager.undo(); // Ctrl+Z in an editor reverts it first
    expect(stale).toBe(2);
    expect(again.undo()).toBe(false);
    expect(cell(doc, id, 0, 0)).toBe('fee bar');
  });
});
