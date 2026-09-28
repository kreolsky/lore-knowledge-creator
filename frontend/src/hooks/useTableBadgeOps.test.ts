/**
 * Unit tests for useTableBadgeOps — the ReferencesPanel table-badge operations,
 * extracted so the delete path is drivable without mounting the whole panel.
 *
 * Pins the identity-scoped contract: the ops resolve the DOCUMENT's entity handle
 * (getEntityHandle(documentId)), never the focused slot — with a reference focused
 * the slot holds the ref's handle and a slot-read delete would drop the wrong ydoc's
 * table (or silently no-op). Null entity handle at delete → explicit toast, not a
 * silent return.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';

const showToast = vi.hoisted(() => vi.fn());
const apiPost = vi.hoisted(() => vi.fn(async () => ({})));

vi.mock('../api/client', () => ({
  apiClient: { post: (...a: unknown[]) => apiPost(...(a as [])) },
}));
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../store/app-store', () => ({
  useAppStore: {
    getState: () => ({ accessLevel: 'full', showToast }),
  },
}));

import * as YjsModel from '../components/editor/live-preview/table-block-model';
import { setEntityHandle } from '../collab/active-handle-registry';
import { releaseHandle, publishHandle } from '../editor/active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import { useTableBadgeOps, type TableBadgeOpsParams } from './useTableBadgeOps';
import type { Document, Reference } from '../types';
import type { CurrentTable } from '../store/app-store';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const { createTable, tableAnchor } = YjsModel;

function makeHandle(doc: Y.Doc): EntityYjsState {
  return { ydoc: doc } as unknown as EntityYjsState;
}

const DOC_ID = 'doc-ops-1';

let container: HTMLDivElement;
let root: Root;
let ops: ReturnType<typeof useTableBadgeOps> | null = null;
// Module-level mocks: beforeEach's clearAllMocks resets their call history but keeps
// the () => null implementation, so every test starts with a fresh null-view getter.
const renamingId = vi.fn();
const nullViewGetter = vi.fn((): EditorView | null => null);

function mountOps(
  currentDocument: Document | null,
  overrides: Partial<TableBadgeOpsParams> = {},
) {
  function Harness() {
    ops = useTableBadgeOps({
      currentDocument,
      currentTable: null,
      setCurrentTable: vi.fn(),
      getView: nullViewGetter,
      tableRenameValue: '',
      setTableRenamingId: renamingId,
      ...overrides,
    } as unknown as TableBadgeOpsParams);
    return createElement('div');
  }
  act(() => root.render(createElement(Harness)));
}

function makeDoc(): Document {
  return { document_id: DOC_ID, title: 'd' } as unknown as Document;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  ops = null;
  vi.clearAllMocks();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  releaseHandle();
  setEntityHandle(DOC_ID, null);
});

describe('useTableBadgeOps — delete targets the entity handle', () => {
  it('deletes the table on the DOCUMENT entity ydoc while the slot holds a foreign handle', async () => {
    const docYdoc = new Y.Doc();
    const t1 = createTable(docYdoc, [['a', 'b']]);
    docYdoc.getText('content').insert(0, tableAnchor('T', t1));
    setEntityHandle(DOC_ID, makeHandle(docYdoc));

    // The slot holds a FOREIGN (reference column) doc with its own table.
    const refYdoc = new Y.Doc();
    const tRef = createTable(refYdoc, [['r1', 'r2']]);
    publishHandle(makeHandle(refYdoc));

    mountOps(makeDoc());
    const entry = { table_id: t1, label: 'T', rows: 1, cols: 2, unlinked: false };
    await act(async () => { await ops!.handleTableDelete(entry); });

    // The DOCUMENT's table is gone; the reference's table is untouched.
    expect(YjsModel.readTableModel(docYdoc, t1)).toBeNull();
    expect(YjsModel.readTableModel(refYdoc, tRef)).not.toBeNull();
    // Checkpoint was requested for the DOCUMENT before the drop.
    expect(apiPost).toHaveBeenCalledWith('/checkpoints', {
      document_id: DOC_ID,
      content: null,
      label: 'tableDeleteBackupLabel',
    });
    docYdoc.destroy();
    refYdoc.destroy();
  });

  it('null entity handle → explicit failure toast, no checkpoint, no mutation', async () => {
    // Slot holds a handle, but the document's ENTITY entry is missing (mount race /
    // conn lost): the delete must NOT fall through to the slot.
    const foreignDoc = new Y.Doc();
    const tForeign = createTable(foreignDoc, [['f']]);
    publishHandle(makeHandle(foreignDoc));

    const docYdoc = new Y.Doc();
    const t1 = createTable(docYdoc, [['a']]);
    setEntityHandle(DOC_ID, null);

    mountOps(makeDoc());
    const entry = { table_id: t1, label: 'T', rows: 1, cols: 1, unlinked: false };
    await act(async () => { await ops!.handleTableDelete(entry); });

    expect(showToast).toHaveBeenCalledWith('tableDeleteFailed', 'error');
    expect(apiPost).not.toHaveBeenCalled();
    expect(YjsModel.readTableModel(foreignDoc, tForeign)).not.toBeNull();
    docYdoc.destroy();
    foreignDoc.destroy();
  });
});

describe('useTableBadgeOps — null-handle ops fail loudly (No silent degradation)', () => {
  it('rename with a null entity handle → toast, rename mode exits, no mutation', () => {
    const foreignDoc = new Y.Doc();
    publishHandle(makeHandle(foreignDoc)); // the slot is NOT a valid fallback
    setEntityHandle(DOC_ID, null);

    const docYdoc = new Y.Doc();
    const t1 = createTable(docYdoc, [['a']]);
    docYdoc.getText('content').insert(0, tableAnchor('Old', t1));

    mountOps(makeDoc(), { tableRenameValue: 'New' });
    act(() => { ops!.handleTableRename({ table_id: t1, label: 'Old', rows: 1, cols: 1, unlinked: false }); });

    expect(showToast).toHaveBeenCalledWith('tableRenameFailed', 'error');
    expect(renamingId).toHaveBeenCalledWith(null);
    // Model and anchor are untouched (a rename would rewrite both).
    expect(docYdoc.getText('content').toString()).toBe(tableAnchor('Old', t1));
    docYdoc.destroy();
    foreignDoc.destroy();
  });

  it('insert with NO live view and a null entity handle → toast, nothing inserted', () => {
    const foreignDoc = new Y.Doc();
    publishHandle(makeHandle(foreignDoc));
    setEntityHandle(DOC_ID, null);

    mountOps(makeDoc()); // getView → null (mount race), entity handle null
    act(() => { ops!.handleTableInsert({ table_id: 't-x', label: 'T', rows: 1, cols: 1, unlinked: false }); });

    expect(showToast).toHaveBeenCalledWith('tableInsertFailed', 'error');
    // No anchor landed in the FOREIGN (slot) ydoc's content.
    expect(foreignDoc.getText('content').toString()).toBe('');
    foreignDoc.destroy();
  });
});
