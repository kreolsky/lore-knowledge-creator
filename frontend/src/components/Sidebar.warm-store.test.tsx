/**
 * Sidebar — the tree fetch must not repaint an already-warm tree.
 *
 * /projects/:id and /docs/:id are top-level route siblings, so crossing between
 * them remounts the whole shell. Sidebar's mount effect then re-ran
 * `GET /projects/:id` and replaced `documents` wholesale — a full tree repaint on
 * every crossing. The guard skips the fetch when the store already holds this
 * project's tree; the project WS channel is what keeps that tree live.
 *
 * These pin BOTH directions: warm + same project ⇒ no request; anything else
 * (cold store, different project) ⇒ the request still happens. A guard tested
 * only in the direction that skips would pass while the tree never loaded at all.
 *
 * Harness: manual createRoot + act with a mocked store, mirroring
 * DocumentTree.test.tsx. The store mock exposes getState because the effect reads
 * it at effect time (deliberately, so `documents` is not an effect dep).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let Sidebar: typeof import('./Sidebar').Sidebar;
let container: HTMLDivElement;
let root: Root;
let getMock: ReturnType<typeof vi.fn>;

/** Mutated by each test BEFORE render — the doMock factories read it lazily. */
let appState: Record<string, unknown>;
/** The route the mocked useDocumentRoute reports. */
let routeProjectId: string | undefined;

const doc = (id: string) => ({
  document_id: id, project_id: 'p1', parent_id: null, title: id,
  is_index: false, content: '', path: '', created_at: '', updated_at: '',
});

beforeEach(async () => {
  vi.resetModules();
  routeProjectId = 'p1';
  getMock = vi.fn(() => Promise.resolve({ documents: [doc('from-network')] }));

  appState = {
    currentProject: null,
    documents: [],
    documentTree: [],
    accessLevel: 'full',
    setDocuments: vi.fn(),
    showToast: vi.fn(),
  };

  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: unknown) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('../store/ui-store', () => {
    const ui = {
      isPublicShare: false, collapsedDocIds: [], toggleDocExpanded: vi.fn(),
      removeDocState: vi.fn(), removeDocStates: vi.fn(),
    };
    return {
      useUIStore: Object.assign(
        (selector?: (s: unknown) => unknown) => (selector ? selector(ui) : ui),
        { getState: () => ui },
      ),
    };
  });
  vi.doMock('../api/client', () => ({ apiClient: { get: getMock, post: vi.fn(), delete: vi.fn() } }));
  vi.doMock('../events', () => ({ emit: vi.fn() }));
  vi.doMock('../hooks/useEvent', () => ({ useEvent: () => {} }));
  vi.doMock('../hooks/useDocumentRoute', () => ({
    useDocumentRoute: () => ({ projectId: routeProjectId, documentId: 'd1' }),
  }));
  vi.doMock('./DocumentTree', () => ({ DocumentTree: () => null }));
  vi.doMock('./DeleteModal', () => ({ DeleteModal: () => null }));
  vi.doMock('./ParentPickerPopup', () => ({ ParentPickerPopup: () => null }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  ({ Sidebar } = await import('./Sidebar'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
  vi.doUnmock('../api/client');
  vi.doUnmock('../events');
  vi.doUnmock('../hooks/useEvent');
  vi.doUnmock('../hooks/useDocumentRoute');
  vi.doUnmock('./DocumentTree');
  vi.doUnmock('./DeleteModal');
  vi.doUnmock('./ParentPickerPopup');
  vi.doUnmock('../i18n');
});

async function mount() {
  await act(async () => { root.render(createElement(Sidebar)); });
}

/** Every GET issued against the project endpoint, regardless of other calls. */
function projectFetches() {
  return getMock.mock.calls.filter(([url]) => String(url).startsWith('/projects/'));
}

describe('Sidebar tree fetch — warm store skips the refetch', () => {
  it('skips GET /projects/:id when the store already holds THIS project tree', async () => {
    appState.currentProject = { project_id: 'p1' };
    appState.documents = [doc('d1')];
    await mount();
    expect(projectFetches()).toHaveLength(0);
    expect(appState.setDocuments).not.toHaveBeenCalled();
  });

  it('still fetches when the store is cold (no documents yet)', async () => {
    appState.currentProject = { project_id: 'p1' };
    appState.documents = [];
    await mount();
    expect(projectFetches()).toEqual([['/projects/p1']]);
  });

  it('still fetches when the warm tree belongs to a DIFFERENT project', async () => {
    // The dangerous direction: skipping here would paint another project's tree.
    appState.currentProject = { project_id: 'p-other' };
    appState.documents = [doc('d1')];
    await mount();
    expect(projectFetches()).toEqual([['/projects/p1']]);
  });

  it('still fetches when documents are warm but no project is set', async () => {
    appState.currentProject = null;
    appState.documents = [doc('d1')];
    await mount();
    expect(projectFetches()).toEqual([['/projects/p1']]);
  });
});

describe('Sidebar tree fetch — a failed load is reported', () => {
  it('toasts failedToLoadTree when GET /projects/:id rejects on a cold store', async () => {
    getMock.mockImplementation(() => Promise.reject(new Error('boom')));
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    await mount();
    expect(appState.showToast).toHaveBeenCalledWith('failedToLoadTree', 'error');
    expect(appState.setDocuments).not.toHaveBeenCalled();
    err.mockRestore();
  });
});
