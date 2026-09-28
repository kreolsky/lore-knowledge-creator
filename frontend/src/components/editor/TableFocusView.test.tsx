/**
 * TableFocusView renders its table through the entity handle registry, NOT the
 * focused slot: "click a table badge while the reference column is focused" nulls
 * the slot (the secondary's teardown) and nothing republishes the doc handle — the
 * focus view must still bind the DOCUMENT's ydoc via getEntityHandle and render the
 * table instead of going blank.
 *
 * Harness: the real component (CodeMirrorEditor + renderExtensions) in jsdom, with
 * the entity registry seeded and the slot explicitly NULL. Static cells render
 * without focus (nested cell editors are lazy), so the mount is light.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import * as Y from 'yjs';
import { createTable, tableAnchor } from './live-preview/table-block-model';
import { setEntityHandle } from '../../collab/active-handle-registry';
import { releaseHandle, publishHandle, getActiveHandle } from '../../editor/active-editor';
import type { EntityYjsState } from '../../collab/yjs-provider';
import type { CurrentTable } from '../../store/app-store';

const setCurrentTable = vi.hoisted(() => vi.fn());

vi.mock('../../store/app-store', () => ({
  useAppStore: (sel: (s: unknown) => unknown) => sel({
    setCurrentTable,
    currentTableLabel: 'TheTable',
    accessLevel: 'full',
    previewDocument: null,
  }),
}));
vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
  t: (k: string) => k,
}));

import { TableFocusView } from './TableFocusView';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOC_ID = 'doc-tf-1';

function makeHandle(doc: Y.Doc): EntityYjsState {
  return { ydoc: doc } as unknown as EntityYjsState;
}

let container: HTMLDivElement;
let root: Root | null = null;

function mountView(table: CurrentTable) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(createElement(TableFocusView, { table })));
}

afterEach(() => {
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
  container?.remove();
  releaseHandle();
  setEntityHandle(DOC_ID, null);
});

describe('TableFocusView — entity-scoped binding (slot null)', () => {
  it('renders the table with the slot null: binds the doc entity handle', async () => {
    const ydoc = new Y.Doc();
    const t1 = createTable(ydoc, [['h1', 'h2'], ['a', 'b']]);
    ydoc.getText('content').insert(0, tableAnchor('TheTable', t1));
    setEntityHandle(DOC_ID, makeHandle(ydoc));
    // The slot is NULL — the reference column's teardown left it unpublished.
    releaseHandle();

    mountView({ document_id: DOC_ID, table_id: t1 });

    // Let CM6 parse + build decorations (double rAF pattern, same as CodeMirrorEditor).
    await act(async () => { await new Promise(r => setTimeout(r, 80)); });

    const tableEl = container.querySelector('table.cm-table-block');
    expect(tableEl).not.toBeNull();
    const cells = container.querySelectorAll('.cm-table-block-cell-static');
    expect([...cells].map((c) => c.textContent)).toEqual(['h1', 'h2', 'a', 'b']);
    ydoc.destroy();
  });

  it('empty entity map: the focus claim publishes NULL, never re-stamps the foreign slot handle', async () => {
    // The doc's entity entry is missing (conn race) while the slot holds the OPEN
    // REFERENCE's handle. claimFocus(handle: docHandle()) writes the slot — passing
    // the slot's own value through would re-stamp the REFERENCE handle as this
    // view's (mis-parented notes/markdown actions). The honest claim is null.
    const refDoc = new Y.Doc();
    publishHandle(makeHandle(refDoc));
    setEntityHandle(DOC_ID, null);

    mountView({ document_id: DOC_ID, table_id: 't-any' });
    await act(async () => { await new Promise(r => setTimeout(r, 80)); });

    expect(getActiveHandle()).toBeNull();
    refDoc.destroy();
  });
});
