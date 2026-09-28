/**
 * Sidebar document delete — subtree mode wiring:
 * - the modal receives the descendant census (childDocs/childRefs) computed from
 *   the live store;
 * - onConfirm(true) optimistically removes the WHOLE subtree, sends
 *   DELETE /documents/{id}?delete_children=true, navigates the open doc to the
 *   nearest surviving ancestor, and cleans UI doc states on success only;
 * - onConfirm(false) (lift mode) removes only the target row and sends
 *   delete_children=false — the open descendant doc is NOT navigated away;
 * - a rejecting DELETE restores the previous documents + error toast (no silent
 *   degradation).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const registered: Record<string, (payload: never) => void> = {};

const DOCS = [
  { document_id: 'd-idx', parent_id: null, title: 'Index', is_index: true },
  { document_id: 'd-target', parent_id: 'd-idx', title: 'Target', is_index: false },
  { document_id: 'd-child', parent_id: 'd-target', title: 'Child', is_index: false },
  { document_id: 'd-grandchild', parent_id: 'd-child', title: 'Grandchild', is_index: false },
  { document_id: 'd-out', parent_id: null, title: 'Outsider', is_index: false },
];

let container: HTMLDivElement;
let root: Root;
let Sidebar: typeof import('./Sidebar').Sidebar;
let deleteMock: ReturnType<typeof vi.fn>;
let getMock: ReturnType<typeof vi.fn>;
let emitMock: ReturnType<typeof vi.fn>;
let setDocuments: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let removeDocStates: ReturnType<typeof vi.fn>;
let onDeleteProp: (id: string, name: string) => void | Promise<void>;
let modalProps: Record<string, unknown> | null;

/** Project-wide references the census fetch returns. r-1 is hosted INSIDE the
 * deleted subtree (on d-child), r-out is hosted outside it. */
const FETCHED_REFS = [
  { reference_id: 'r-1', document_id: 'd-child' },
  { reference_id: 'r-out', document_id: 'd-out' },
];

beforeEach(async () => {
  vi.resetModules();
  deleteMock = vi.fn().mockResolvedValue(undefined);
  getMock = vi.fn((url: string) => {
    if (url.startsWith('/references?project_id=')) return Promise.resolve(FETCHED_REFS);
    return Promise.resolve({ documents: [] });
  });
  emitMock = vi.fn();
  setDocuments = vi.fn();
  showToast = vi.fn();
  removeDocStates = vi.fn();
  modalProps = null;
  for (const k of Object.keys(registered)) delete registered[k];

  vi.doMock('../api/client', () => ({
    apiClient: { get: getMock, post: vi.fn(), patch: vi.fn(), delete: deleteMock },
  }));
  vi.doMock('../events', () => ({ emit: emitMock }));
  vi.doMock('../hooks/useEvent', () => ({
    useEvent: (name: string, cb: (payload: never) => void) => { registered[name] = cb; },
  }));
  // d-grandchild is the OPEN doc — it sits inside the deleted subtree.
  vi.doMock('../hooks/useDocumentRoute', () => ({
    useDocumentRoute: () => ({ projectId: 'p1', documentId: 'd-grandchild' }),
  }));
  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('./DocumentTree', () => ({
    DocumentTree: (props: { onDelete: typeof onDeleteProp }) => { onDeleteProp = props.onDelete; return null; },
  }));
  vi.doMock('./DeleteModal', () => ({
    DeleteModal: (props: Record<string, unknown>) => { modalProps = props; return null; },
  }));
  vi.doMock('./ParentPickerPopup', () => ({ ParentPickerPopup: () => null }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  const appState = {
    accessLevel: 'full',
    documents: DOCS,
    // Deliberately EMPTY: the ref census must come from the project-wide fetch,
    // not this scope-filtered store list (live-drive finding 2026-08-24).
    references: [],
    setDocuments,
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  const uiState = {
    isPublicShare: false, collapsedDocIds: [], toggleDocExpanded: vi.fn(),
    removeDocState: vi.fn(), removeDocStates,
  };
  vi.doMock('../store/ui-store', () => ({
    useUIStore: Object.assign(
      (selector: (s: typeof uiState) => unknown) => selector(uiState),
      { getState: () => uiState },
    ),
  }));

  ({ Sidebar } = await import('./Sidebar'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../api/client');
  vi.doUnmock('../events');
  vi.doUnmock('../hooks/useEvent');
  vi.doUnmock('../hooks/useDocumentRoute');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('./DocumentTree');
  vi.doUnmock('./DeleteModal');
  vi.doUnmock('./ParentPickerPopup');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

function openModal() {
  act(() => { root.render(createElement(Sidebar)); });
}

/** handleDelete is async (ref census fetch) — await the modal-open. */
async function openDeleteModal(id = 'd-target', name = 'Target') {
  act(() => { root.render(createElement(Sidebar)); });
  await act(async () => { await onDeleteProp(id, name); });
}

describe('Sidebar delete — subtree mode', () => {
  it('modal receives the descendant census (refs from the project-wide fetch) and defaults to subtree', async () => {
    await openDeleteModal();
    expect(getMock).toHaveBeenCalledWith('/references?project_id=p1&limit=1000');
    expect(modalProps).not.toBeNull();
    expect(modalProps!.subtreeOption).toEqual({ childDocs: 2, childRefs: 1 });

    await act(async () => {
      await (modalProps!.onConfirm as (c?: boolean) => Promise<void>)(true);
    });

    expect(deleteMock).toHaveBeenCalledWith('/documents/d-target?delete_children=true');
    expect(setDocuments).toHaveBeenCalledWith(
      DOCS.filter(d => !['d-target', 'd-child', 'd-grandchild'].includes(d.document_id)),
    );
    expect(emitMock).toHaveBeenCalledWith('document-deleted', { documentId: 'd-target' });
    // Open doc is inside the deleted set → nearest SURVIVING ancestor of the target.
    expect(emitMock).toHaveBeenCalledWith('navigate-to-document', { documentId: 'd-idx' });
    // Success branch only: UI states of every subtree id are dropped.
    expect(removeDocStates).toHaveBeenCalledWith(['d-target', 'd-child', 'd-grandchild']);
  });

  it('lift mode removes only the target row and reparents its children optimistically', async () => {
    await openDeleteModal();
    await act(async () => {
      await (modalProps!.onConfirm as (c?: boolean) => Promise<void>)(false);
    });

    expect(deleteMock).toHaveBeenCalledWith('/documents/d-target?delete_children=false');
    // The lifted children survive with parent_id moved to the target's parent —
    // keeping them pointed at the removed row orphans them in the tree builder.
    // Only DIRECT children lift (d-child); deeper descendants keep their parent.
    const lifted = [
      DOCS[0],
      { ...DOCS[2], parent_id: 'd-idx' },
      DOCS[3],
      DOCS[4],
    ];
    expect(setDocuments).toHaveBeenCalledWith(lifted);
    expect(emitMock).not.toHaveBeenCalledWith('navigate-to-document', expect.anything());
  });

  it('a rejecting DELETE restores the documents and shows the error toast', async () => {
    deleteMock.mockRejectedValue(new Error('boom'));
    await openDeleteModal();
    await act(async () => {
      await (modalProps!.onConfirm as (c?: boolean) => Promise<void>)(true);
    });

    expect(setDocuments).toHaveBeenLastCalledWith(DOCS);
    expect(showToast).toHaveBeenCalledWith('failedToDeleteDocument', 'error');
    expect(removeDocStates).not.toHaveBeenCalled();
  });

  it('a failing ref-census fetch falls back to the store count and still opens the modal', async () => {
    getMock.mockImplementation((url: string) =>
      url.startsWith('/references?project_id=')
        ? Promise.reject(new Error('net'))
        : Promise.resolve({ documents: [] }));
    await openDeleteModal();
    // Store list is empty in this fixture → best-effort 0; the modal still opens
    // with the doc census and the subtree checkbox.
    expect(modalProps!.subtreeOption).toEqual({ childDocs: 2, childRefs: 0 });
  });

  it('leaf doc without hosted refs opens the modal without subtreeOption', async () => {
    await openDeleteModal();
    // d-grandchild: no children; neither fetched ref is hosted on it — a leaf.
    await act(async () => { await onDeleteProp('d-grandchild', 'Grandchild'); });
    expect(modalProps!.subtreeOption).toBeUndefined();
  });
});
