/**
 * Sidebar — cross-project move reactions (SYSTEM: project-ws):
 * - 'ws:documents_moved_out' drops the moved ids from the tree + doc states;
 * - when the OPEN doc is among them: toast + hard follow via
 *   window.location.assign('/docs/<id>') (both WS channels and the collab join
 *   set are keyed by the old project — reload, not in-app navigation);
 * - 'ws:documents_moved_in' refetches GET /projects/:id and replaces the
 *   tree wholesale.
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
  { document_id: 'd-moved', parent_id: 'd-idx', title: 'Moved', is_index: false },
  { document_id: 'd-child', parent_id: 'd-moved', title: 'Child', is_index: false },
  { document_id: 'd-out', parent_id: null, title: 'Outsider', is_index: false },
];

let container: HTMLDivElement;
let root: Root;
let Sidebar: typeof import('./Sidebar').Sidebar;
let getMock: ReturnType<typeof vi.fn>;
let setDocuments: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let removeDocStates: ReturnType<typeof vi.fn>;
let assignMock: ReturnType<typeof vi.fn>;
let openDocId: string | null;

beforeEach(async () => {
  vi.resetModules();
  getMock = vi.fn(() => Promise.resolve({ documents: [] }));
  setDocuments = vi.fn();
  showToast = vi.fn();
  removeDocStates = vi.fn();
  assignMock = vi.fn();
  openDocId = 'd-out';
  for (const k of Object.keys(registered)) delete registered[k];

  // jsdom cannot navigate; replace location with a stub capturing assign().
  Object.defineProperty(window, 'location', {
    value: { assign: assignMock, href: 'http://localhost/' },
    writable: true,
  });

  vi.doMock('../api/client', () => ({
    apiClient: { get: getMock, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  }));
  vi.doMock('../events', () => ({ emit: vi.fn() }));
  vi.doMock('../hooks/useEvent', () => ({
    useEvent: (name: string, cb: (payload: never) => void) => { registered[name] = cb; },
  }));
  vi.doMock('../hooks/useDocumentRoute', () => ({
    useDocumentRoute: () => ({ projectId: 'p1', documentId: 'doc-open' }),
  }));
  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('./DocumentTree', () => ({ DocumentTree: () => null }));
  vi.doMock('./DeleteModal', () => ({ DeleteModal: () => null }));
  vi.doMock('./ParentPickerPopup', () => ({ ParentPickerPopup: () => null }));
  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string, vars?: Record<string, string>) =>
      vars ? `${k}:${JSON.stringify(vars)}` : k }),
  }));

  const appState = () => ({
    accessLevel: 'full',
    documents: DOCS,
    references: [],
    currentDocument: openDocId ? { document_id: openDocId } : null,
    setDocuments,
    showToast,
  });
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: ReturnType<typeof appState>) => unknown) => selector(appState()),
      { getState: () => appState() },
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
  act(() => { root.render(createElement(Sidebar)); });
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

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

describe('Sidebar — ws:documents_moved_out', () => {
  it('drops the moved ids from the tree and the doc states', async () => {
    act(() => {
      registered['ws:documents_moved_out']({
        document_ids: ['d-moved', 'd-child'],
        reference_ids: [],
        target_project_id: 'p2',
        target_project_name: 'Target',
      } as never);
    });
    expect(setDocuments).toHaveBeenCalledWith(
      DOCS.filter(d => !['d-moved', 'd-child'].includes(d.document_id)),
    );
    expect(removeDocStates).toHaveBeenCalledWith(['d-moved', 'd-child']);
    // Open doc (d-out) not in the set → no toast, no reload.
    expect(showToast).not.toHaveBeenCalled();
    expect(assignMock).not.toHaveBeenCalled();
  });

  it('toasts and hard-follows the OPEN doc to /docs/<id> when it is in the set', async () => {
    openDocId = 'd-child';
    act(() => {
      registered['ws:documents_moved_out']({
        document_ids: ['d-moved', 'd-child'],
        reference_ids: [],
        target_project_id: 'p2',
        target_project_name: 'Target',
      } as never);
    });
    expect(showToast).toHaveBeenCalledWith(
      'documentMovedToProject:{"project":"Target"}', 'info',
    );
    expect(assignMock).toHaveBeenCalledWith('/docs/d-child');
  });
});

describe('Sidebar — ws:documents_moved_in', () => {
  it('refetches GET /projects/:id and replaces the tree wholesale', async () => {
    const fresh = [{ document_id: 'd-idx', parent_id: null, title: 'Index', is_index: true }];
    getMock.mockResolvedValue({ documents: fresh });
    act(() => {
      registered['ws:documents_moved_in']({ document_ids: ['d-new'] } as never);
    });
    await flush();
    expect(getMock).toHaveBeenCalledWith('/projects/p1');
    expect(setDocuments).toHaveBeenCalledWith(fresh);
  });
});
