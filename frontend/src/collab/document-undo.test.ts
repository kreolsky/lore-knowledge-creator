/** The document-wide undo stack over real editors bound to one Y.Doc: step boundaries and what is never undone. */
// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import * as Y from 'yjs';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { createDocumentUndo } from './document-undo';
import { createYjsExtension } from './yjs-binding';
import {
  addColumn,
  createTable,
  getCellText,
  orderedColIds,
  orderedRowIds,
  removeRow,
  TABLE_LOCAL_ORIGIN,
} from '../components/editor/live-preview/table-block-model';
import { applyTablesJson, serializeTables } from '../components/editor/live-preview/table-serialize';
import { DEFAULT_COLUMN_WIDTH } from '../components/editor/live-preview/table-widths';
import { REPLACE_ALL_ORIGIN } from '../components/editor/find-replace-undo';

const views: EditorView[] = [];
afterEach(() => { while (views.length) views.pop()!.destroy(); });

function bind(ytext: Y.Text, manager: Y.UndoManager): EditorView {
  const view = new EditorView({
    parent: document.body.appendChild(document.createElement('div')),
    state: EditorState.create({ doc: ytext.toString(), extensions: createYjsExtension(ytext, manager) }),
  });
  views.push(view);
  return view;
}

function seed() {
  const doc = new Y.Doc();
  const id = createTable(doc, [['h1', 'h2'], ['a', 'b']]);
  const manager = createDocumentUndo(doc, [TABLE_LOCAL_ORIGIN, REPLACE_ALL_ORIGIN]);
  const cell = (r: number, c: number) => getCellText(doc, id, orderedRowIds(doc, id)![r], orderedColIds(doc, id)![c])!;
  return { doc, id, manager, cell };
}

const type = (view: EditorView, text: string) =>
  view.dispatch({ changes: { from: view.state.doc.length, insert: text }, userEvent: 'input.type' });

describe('document-wide undo', () => {
  // TEST-WHY: pure-logic-edge — the grouping contract: a host edit, then a cell edit inside the 500 ms capture window (split by the focus change), then add column are THREE Ctrl+Z steps on ONE stack, undone newest first.
  it('host edit, cell edit and a structural op are three separate steps', () => {
    const { doc, id, manager, cell } = seed();
    const host = bind(doc.getText('content'), manager);
    const cellView = bind(cell(1, 0), manager);
    type(host, 'H');
    cellView.contentDOM.dispatchEvent(new FocusEvent('focus'));
    type(cellView, 'C');
    addColumn(doc, id, DEFAULT_COLUMN_WIDTH);
    expect(manager.undoStack).toHaveLength(3);

    manager.undo();
    expect(orderedColIds(doc, id)).toHaveLength(2);
    expect(cell(1, 0).toString()).toBe('aC');
    manager.undo();
    expect(cell(1, 0).toString()).toBe('a');
    expect(cellView.state.doc.toString()).toBe('a');
    expect(host.state.doc.toString()).toBe('H');
    manager.undo();
    expect(host.state.doc.toString()).toBe('');
    expect(manager.undoStack).toHaveLength(0);
  });

  // TEST-WHY: pure-logic-edge — the trackedOrigins rule (document-undo.ts) — a remote update, applyTablesJson seeding and an origin-less local write never enter the stack; and undoing a row removal restores the row with its cells' text.
  it('never captures remote or seeding writes, and restores a removed row', () => {
    const { doc, id, manager, cell } = seed();
    const peer = new Y.Doc();
    Y.applyUpdate(peer, Y.encodeStateAsUpdate(doc));
    peer.getText('content').insert(0, 'peer ');
    Y.applyUpdate(doc, Y.encodeStateAsUpdate(peer, Y.encodeStateVector(doc)), 'remote');
    applyTablesJson(doc, serializeTables(doc)); // a snapshot re-seed rebuilds every table
    doc.getText('content').insert(0, 'raw ');
    expect(manager.undoStack).toHaveLength(0);

    const before = serializeTables(doc);
    removeRow(doc, id, orderedRowIds(doc, id)![1]);
    expect(orderedRowIds(doc, id)).toHaveLength(1);
    manager.undo();
    expect(orderedRowIds(doc, id)).toHaveLength(2);
    expect([cell(1, 0).toString(), cell(1, 1).toString()]).toEqual(['a', 'b']);
    expect(serializeTables(doc)).toBe(before);
    expect(doc.getText('content').toString()).toBe('raw peer ');
  });
});
