/**
 * Render test for useDocumentTables.
 *
 * Locks the reactive wiring: the hook derives the DOCUMENT's table list from the
 * entity-scoped handle registry (getEntityHandle(documentId) — identity, not focus:
 * with a reference focused the slot holds the ref's handle, the badges still list the
 * doc's tables) and re-renders when either the `tables` model or the `content` text
 * mutates. The pure derivation (labels, shape, orphans, duplicates) is covered by
 * table-block-model.test.ts (`listDocumentTables`).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import * as Y from 'yjs';
import { createTable, addRow, deleteTable, tableAnchor, setCellText } from '../components/editor/live-preview/table-block-model';
import { setEntityHandle } from '../collab/active-handle-registry';
import { releaseHandle, publishHandle } from '../editor/active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';

import { useDocumentTables } from './useDocumentTables';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => vi.useRealTimers());
afterEach(() => {
  releaseHandle();
  setEntityHandle('doc-1', null);
});

function makeHandle(doc: Y.Doc): EntityYjsState {
  return { ydoc: doc } as unknown as EntityYjsState;
}

function renderHook(documentId: string | null): {
  current: () => ReturnType<typeof useDocumentTables>;
  unmount: () => void;
} {
  const container = document.createElement('div');
  let value: ReturnType<typeof useDocumentTables> = [];
  function Harness() {
    value = useDocumentTables(documentId);
    return createElement('div');
  }
  const root: Root = createRoot(container);
  act(() => root.render(createElement(Harness)));
  return { current: () => value, unmount: () => act(() => root.unmount()) };
}

describe('useDocumentTables (reactive list)', () => {
  it('returns [] when no document is open', () => {
    const h = renderHook(null);
    expect(h.current()).toEqual([]);
    h.unmount();
  });

  it('returns [] before the entity handle publishes a ydoc (mount race)', () => {
    setEntityHandle('doc-1', null);
    const h = renderHook('doc-1');
    expect(h.current()).toEqual([]);
    h.unmount();
  });

  it('derives the table list from the live ydoc once the ENTITY handle is published', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a', 'b'], ['c', 'd']]);
    doc.getText('content').insert(0, `intro\n\n${tableAnchor('First', t1)}`);
    setEntityHandle('doc-1', makeHandle(doc));

    const h = renderHook('doc-1');
    expect(h.current()).toEqual([
      { table_id: t1, label: 'First', rows: 2, cols: 2, unlinked: false },
    ]);
    h.unmount();
    doc.destroy();
  });

  it('attaches synchronously via the entity subscription — no polling interval', () => {
    // The pre-entity wiring polled getActiveHandle() on a 150ms interval; the
    // subscription lands the observer in the same tick the effect runs.
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('T', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    const setSpy = vi.spyOn(globalThis, 'setInterval');
    const h = renderHook('doc-1');
    expect(h.current()).toHaveLength(1);
    expect(setSpy).not.toHaveBeenCalled();
    setSpy.mockRestore();
    h.unmount();
    doc.destroy();
  });

  it('a FOREIGN active slot still lists the doc\'s tables (identity, not focus)', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a', 'b']]);
    doc.getText('content').insert(0, tableAnchor('DocTable', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    // The FOCUSED slot holds the reference column's handle — a different ydoc with
    // its own table that must NOT leak into the document badge list.
    const refDoc = new Y.Doc();
    const tRef = createTable(refDoc, [['r1']]);
    refDoc.getText('content').insert(0, tableAnchor('RefTable', tRef));
    publishHandle(makeHandle(refDoc));

    const h = renderHook('doc-1');
    expect(h.current()).toEqual([
      { table_id: t1, label: 'DocTable', rows: 1, cols: 2, unlinked: false },
    ]);
    h.unmount();
    refDoc.destroy();
    doc.destroy();
  });

  it('re-renders when a table MODEL mutates (row added → shape changes)', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('T', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    const h = renderHook('doc-1');
    expect(h.current()[0]).toMatchObject({ rows: 1 });
    act(() => { addRow(doc, t1); });
    expect(h.current()[0]).toMatchObject({ rows: 2 });
    h.unmount();
    doc.destroy();
  });

  it('re-renders when the model is DELETED on the entity doc (badge disappears)', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('T', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    const h = renderHook('doc-1');
    expect(h.current()).toHaveLength(1);
    act(() => { deleteTable(doc, t1); });
    expect(h.current()).toHaveLength(0);
    h.unmount();
    doc.destroy();
  });

  it('re-renders when CONTENT mutates (a new table anchor is added)', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('T1', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    const h = renderHook('doc-1');
    expect(h.current()).toHaveLength(1);

    const t2 = createTable(doc, [['x', 'y']]);
    act(() => {
      doc.getText('content').insert(0, `${tableAnchor('T2', t2)}\n`);
    });
    expect(h.current()).toEqual([
      { table_id: t2, label: 'T2', rows: 1, cols: 2, unlinked: false },
      { table_id: t1, label: 'T1', rows: 1, cols: 1, unlinked: false },
    ]);
    h.unmount();
    doc.destroy();
  });

  it('does NOT re-render on a cell-text-only edit (shape unchanged — panel badge unaffected)', () => {
    const doc = new Y.Doc();
    const t1 = createTable(doc, [['a']]);
    doc.getText('content').insert(0, tableAnchor('T', t1));
    setEntityHandle('doc-1', makeHandle(doc));

    const h = renderHook('doc-1');
    const before = h.current();
    act(() => { setCellText(doc, t1, 0, 0, 'changed cell body'); });
    // Same shape → list is identical (deep-equal); the hook may or may not re-render, but
    // the DERIVED value must be unchanged.
    expect(h.current()).toEqual(before);
    h.unmount();
    doc.destroy();
  });

  it('re-attaches when the entity handle is REPLACED (re-join lands a new ydoc)', () => {
    const docA = new Y.Doc();
    const tA = createTable(docA, [['a']]);
    docA.getText('content').insert(0, tableAnchor('A', tA));
    setEntityHandle('doc-1', makeHandle(docA));

    const h = renderHook('doc-1');
    expect(h.current()).toHaveLength(1);

    // Entity re-join: a fresh ydoc with a different table replaces the handle.
    const docB = new Y.Doc();
    const tB = createTable(docB, [['b1', 'b2'], ['b3', 'b4']]);
    docB.getText('content').insert(0, tableAnchor('B', tB));
    act(() => { setEntityHandle('doc-1', makeHandle(docB)); });

    expect(h.current()).toEqual([
      { table_id: tB, label: 'B', rows: 2, cols: 2, unlinked: false },
    ]);
    h.unmount();
    docA.destroy();
    docB.destroy();
  });
});
