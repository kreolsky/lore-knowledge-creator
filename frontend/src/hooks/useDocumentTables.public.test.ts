/**
 * useDocumentTables — PUBLIC branch (static seed from currentDocument).
 *
 * On /docs/<id> there is no collab ydoc: PublicEditor seeds a preview-local
 * Y.Doc from `tables_json` and never publishes a handle, so the panel's badges
 * must derive statically from `currentDocument.content` + `currentDocument.tables_json`
 * (plan public-refs-panel-scope-parity): linked tables in document/anchor order,
 * unlinked models appended below (mirrors `listDocumentTables`), no polling of
 * `getActiveHandle`, and the throwaway Y.Doc is disposed after the derive.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import * as Y from 'yjs';
import { tableAnchor } from '../components/editor/live-preview/table-block-model';
import { releaseHandle, publishHandle } from '../editor/active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import type { Document } from '../types';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';

import { useDocumentTables } from './useDocumentTables';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeEach(() => vi.useRealTimers());
afterEach(() => releaseHandle());

const NOW = '2026-01-01T00:00:00Z';

function mkDoc(id: string, content: string, tables_json: string | null): Document {
  return {
    document_id: id, project_id: '', parent_id: null, title: id, content, path: '',
    is_index: false, created_at: NOW, updated_at: NOW, tables_json,
  };
}

// t1/t2 linked via anchors (t2 appears FIRST in content — anchor order wins);
// t3 present only in tables_json → unlinked, appended below.
const CONTENT = `# H

${tableAnchor('Second', 't2')}

${tableAnchor('First', 't1')}
`;
const TABLES_JSON = JSON.stringify({
  t1: { columns: [100, 100], rows: [['a', 'b'], ['c', 'd']] },
  t2: { columns: [100], rows: [['x']] },
  t3: { columns: [100, 100], rows: [['unlinked']] },
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

describe('useDocumentTables (public static branch)', () => {
  it('derives badges from content anchors + tables_json: linked in anchor order, unlinked appended', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: mkDoc('doc-1', CONTENT, TABLES_JSON) });

    const h = renderHook('doc-1');
    expect(h.current()).toEqual([
      { table_id: 't2', label: 'Second', rows: 1, cols: 1, unlinked: false },
      { table_id: 't1', label: 'First', rows: 2, cols: 2, unlinked: false },
      { table_id: 't3', label: '', rows: 1, cols: 2, unlinked: true },
    ]);
    h.unmount();
  });

  it('returns [] when no document is open on public', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: null });
    const h = renderHook(null);
    expect(h.current()).toEqual([]);
    h.unmount();
  });

  it('does NOT poll getActiveHandle on public (no collab ydoc will ever arrive)', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: mkDoc('doc-1', CONTENT, TABLES_JSON) });

    const setSpy = vi.spyOn(globalThis, 'setInterval');
    const h = renderHook('doc-1');
    expect(setSpy.mock.calls.some(c => c[1] === 150)).toBe(false);
    setSpy.mockRestore();
    h.unmount();
  });

  it('ignores a published active handle on public (static seed is authoritative)', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: mkDoc('doc-1', CONTENT, TABLES_JSON) });

    // A stale handle from a previous authed session must not leak in.
    const foreign = new Y.Doc();
    foreign.getText('content').insert(0, tableAnchor('Foreign', 'fx'));
    // (no model for fx → even the authed branch would list nothing; publish one
    // via the tables map to make the handle genuinely listable)
    const fx = new Y.Map() as never;
    foreign.getMap('tables').set('fx', fx);
    publishHandle(makeHandle(foreign));

    const h = renderHook('doc-1');
    expect(h.current().map(e => e.table_id)).toEqual(['t2', 't1', 't3']);
    h.unmount();
    foreign.destroy();
  });

  it('re-derives when the document switches (doc-scoped, survives an open reference)', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: mkDoc('doc-1', CONTENT, TABLES_JSON) });

    const otherJson = JSON.stringify({ u1: { columns: [80], rows: [['u']] } });
    const other = mkDoc('doc-2', tableAnchor('Only', 'u1'), otherJson);

    // Mirror the panel's real wiring: the id comes from the same store slice as
    // the payload, so a doc switch re-renders with a matching id pair.
    const container = document.createElement('div');
    let value: ReturnType<typeof useDocumentTables> = [];
    function Harness() {
      const docId = useAppStore(s => s.currentDocument?.document_id ?? null);
      value = useDocumentTables(docId);
      return createElement('div');
    }
    const r: Root = createRoot(container);
    act(() => r.render(createElement(Harness)));

    expect(value).toHaveLength(3);
    act(() => { useAppStore.setState({ currentDocument: other }); });
    expect(value).toEqual([
      { table_id: 'u1', label: 'Only', rows: 1, cols: 1, unlinked: false },
    ]);
    act(() => r.unmount());
  });

  it('disposes the throwaway Y.Doc after deriving (no leak per re-seed)', () => {
    useUIStore.setState({ isPublicShare: true });
    useAppStore.setState({ currentDocument: mkDoc('doc-1', CONTENT, TABLES_JSON) });

    const destroySpy = vi.spyOn(Y.Doc.prototype, 'destroy');
    const h = renderHook('doc-1');
    expect(h.current()).toHaveLength(3);
    expect(destroySpy).toHaveBeenCalled();
    destroySpy.mockRestore();
    h.unmount();
  });
});
