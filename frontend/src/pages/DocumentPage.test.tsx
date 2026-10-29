/**
 * Unit tests for DocumentPage — the URL-driven open contracts marked
 * ARCH/INVARIANT in DocumentPage.tsx: the concurrent open-and-commit effect
 * (cache-fresher merge, staleness guard, already-current skip, failure
 * bounce), the bare /docs/:id one-shot bundle path (project + my_access from
 * ONE payload, hydrateSessions from the bundle, toast+navigate on failure),
 * and the split/focus layout matrix.
 *
 * Harness: manual createRoot + act per the repo's component-test pattern
 * (ProjectPage.test.tsx). resolveRestoredReference stays REAL (binds the
 * persisted-pointer contract); apiClient, stores and heavy children are mocked.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ---- route / nav ----
const routeState: Record<string, unknown> = { projectId: 'p1', documentId: 'd-1' };
vi.mock('../hooks/useDocumentRoute', () => ({ useDocumentRoute: () => routeState }));
const navigate = vi.fn();
let pathname = '/projects/p1/docs/d-1';
vi.mock('react-router-dom', () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname }),
}));

// ---- stores ----
const appState: Record<string, unknown> = {
  currentProject: { project_id: 'p1' },
  currentDocument: null,
  currentUser: { user_id: 'u1' },
  documents: [] as Array<Record<string, unknown>>,
  currentReference: null,
  currentTable: null,
  previewDocument: null,
  snapshotPreview: null,
  setCurrentDocument: vi.fn(),
  setCurrentProject: vi.fn(),
  setAccessLevel: vi.fn(),
  showToast: vi.fn(),
};
vi.mock('../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));

const docStates: Record<string, { splitView?: boolean; refOpenMode?: 'center' | 'split' | 'panel' }> = {};
const uiGetState: Record<string, unknown> = {
  projectPrefsLoaded: false,
  loadProjectPrefs: vi.fn(),
  awaitProjectPrefs: vi.fn().mockResolvedValue(undefined),
  markPublicFallback: vi.fn(),
  getCurrentReferenceForDoc: vi.fn(() => null),
  setCurrentReferenceForDoc: vi.fn(),
};
vi.mock('../store/ui-store', () => ({
  useDocState: (docId: string | null) => docStates[docId ?? ''] ?? { splitView: false },
  useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiGetState), { getState: () => uiGetState }),
}));

const noteChatGetState: Record<string, unknown> = {
  loadSessions: vi.fn().mockResolvedValue(undefined),
  // The guard branch calls this on the already-committed skip (pre-commit nav
  // path); without the entry the existing guard-branch tests crash with TypeError.
  ensureSessions: vi.fn().mockResolvedValue(undefined),
  hydrateSessions: vi.fn(),
  setActiveSession: vi.fn(),
  setPendingInputFocus: vi.fn(),
};
vi.mock('../store/note-chat-store', () => ({
  useNoteChatStore: Object.assign((sel: (s: unknown) => unknown) => sel(noteChatGetState), { getState: () => noteChatGetState }),
}));

const noteStoreState: Record<string, unknown> = {
  setActiveNoteThreadId: vi.fn(),
};
vi.mock('../store/note-store', () => ({
  useNoteStore: Object.assign((sel: (s: unknown) => unknown) => sel(noteStoreState), { getState: () => noteStoreState }),
}));

// ---- IO ----
const apiGet = vi.fn();
// The real error taxonomy comes through: the open path decides the mirror deletion with
// isAccessRefusal over real ForbiddenError/HttpError instances.
vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  apiClient: { get: (...a: unknown[]) => apiGet(...a) },
}));
const clearEntityMirrors = vi.fn();
vi.mock('../collab/local-doc-persistence', () => ({
  clearLocalDocsForEntity: (id: string) => clearEntityMirrors(id),
}));
const loadReferences = vi.fn().mockResolvedValue([]);
vi.mock('../api/references-fetch', () => ({ loadReferences: (...a: unknown[]) => loadReferences(...a) }));

// ---- events ----
const eventHandlers: Record<string, (p: unknown) => void> = {};
vi.mock('../hooks/useEvent', () => ({ useEvent: (name: string, cb: (p: unknown) => void) => { eventHandlers[name] = cb; } }));

vi.mock('../i18n', () => ({ t: (k: string) => k }));

// ---- heavy children → markers ----
const splitProps: unknown[] = [];
vi.mock('../components/editor/SplitEditorLayout', () => ({
  SplitEditorLayout: (props: unknown) => {
    splitProps.push(props);
    return createElement('div', { 'data-testid': 'split-layout' });
  },
}));
const tableProps: unknown[] = [];
vi.mock('../components/editor/TableFocusView', () => ({
  TableFocusView: (props: unknown) => {
    tableProps.push(props);
    return createElement('div', { 'data-testid': 'table-focus' });
  },
}));
const editorProps: unknown[] = [];
vi.mock('../components/Editor', () => ({
  Editor: (props: unknown) => {
    editorProps.push(props);
    return createElement('div', { 'data-testid': 'editor' });
  },
}));

import { DocumentPage } from './DocumentPage';

let root: Root | null = null;
function mountPage() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(DocumentPage)));
  return host;
}
function unmount() {
  // Capture into a local: `root` is a mutable module-level binding, so TS drops
  // the null-narrowing inside the act() closure.
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

const SERVER_DOC = { document_id: 'd-1', title: 'Doc', content: 'server', headings: [], updated_at: '2026-08-19T00:00:00Z' };

beforeEach(() => {
  vi.clearAllMocks();
  apiGet.mockResolvedValue({ ...SERVER_DOC });
  loadReferences.mockResolvedValue([]);
  navigate.mockReset();
  pathname = '/projects/p1/docs/d-1';
  routeState.projectId = 'p1';
  routeState.documentId = 'd-1';
  appState.currentProject = { project_id: 'p1' };
  appState.currentDocument = null;
  appState.documents = [];
  appState.currentReference = null;
  appState.currentTable = null;
  appState.previewDocument = null;
  appState.snapshotPreview = null;
  Object.keys(docStates).forEach((k) => delete docStates[k]);
  uiGetState.projectPrefsLoaded = false;
  uiGetState.getCurrentReferenceForDoc = vi.fn(() => null);
  splitProps.length = 0;
  tableProps.length = 0;
  Object.keys(eventHandlers).forEach((k) => delete eventHandlers[k]);
});

describe('DocumentPage — concurrent open effect', () => {

  it('a refused per-id fetch falls back to the public view, not a bounce', async () => {
    const { ForbiddenError } = await import('../api/client');
    apiGet.mockRejectedValue(new ForbiddenError());
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(uiGetState.markPublicFallback).toHaveBeenCalledWith(['d-1']);
    expect(navigate).not.toHaveBeenCalled();
    unmount();
  });
});

describe('DocumentPage — pre-committed open (guard branch)', () => {
  // The pre-commit nav path (Header.setCurrentDocument BEFORE navigate) mounts the
  // page with the document already committed: the open-bundle effect early-returns
  // at the guard, so the guard branch itself must reconcile note sessions — it is
  // the only loader on that path. In the real store ensureSessions fetches only on
  // scope mismatch (note-chat-store tests); here the mock records the call.
  it('calls ensureSessions(projectId, documentId) when the doc is already committed (no doc re-fetch)', () => {
    appState.currentDocument = { document_id: 'd-1' };
    mountPage();
    expect(noteChatGetState.ensureSessions).toHaveBeenCalledWith('p1', 'd-1');
    expect(apiGet).not.toHaveBeenCalled();
    unmount();
  });

  it('skips ensureSessions when no projectId is resolvable', () => {
    appState.currentDocument = { document_id: 'd-1' };
    routeState.projectId = undefined;
    mountPage();
    expect(noteChatGetState.ensureSessions).not.toHaveBeenCalled();
    expect(apiGet).not.toHaveBeenCalled();
    unmount();
  });

  it('same-doc remount stays zero-fetch at the page level; the store dedupes the ensureSessions fetch', () => {
    appState.currentDocument = { document_id: 'd-1' };
    mountPage();
    unmount();
    mountPage();
    expect(noteChatGetState.ensureSessions).toHaveBeenCalledTimes(2);
    expect(apiGet).not.toHaveBeenCalled();
    unmount();
  });
});

describe('DocumentPage — bare /docs/:id cold path', () => {
  beforeEach(() => {
    pathname = '/docs/d-1';
    appState.currentProject = null; // cold: not yet hydrated
  });

  it('bundle failure: toast + bounce to landing (no silent degradation)', async () => {
    apiGet.mockRejectedValue(new Error('403'));
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(appState.showToast).toHaveBeenCalledWith('documentOpenFailed', 'error');
    expect(navigate).toHaveBeenCalledWith('/');
    unmount();
  });

  it('a refused open hands the id to the public-share fallback instead of bouncing', async () => {
    const { ForbiddenError } = await import('../api/client');
    apiGet.mockRejectedValue(new ForbiddenError());
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    // A published document must open for a signed-in non-member exactly as it does
    // for a logged-out visitor — see the publicFallbackDocIds in ui-store.
    expect(uiGetState.markPublicFallback).toHaveBeenCalledWith(['d-1']);
    expect(appState.showToast).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    unmount();
  });

  it('a refused open deletes this document\'s local mirror; an outage keeps it', async () => {
    const { ForbiddenError, HttpError } = await import('../api/client');
    // 403 arrives as ForbiddenError — no `status` field, which is the whole trap.
    apiGet.mockRejectedValue(new ForbiddenError());
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(clearEntityMirrors).toHaveBeenCalledWith('d-1');
    unmount();

    clearEntityMirrors.mockClear();
    apiGet.mockRejectedValue(new HttpError(503, 'Service Unavailable'));
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(clearEntityMirrors).not.toHaveBeenCalled();
    unmount();
  });
});

describe('DocumentPage — layout matrix', () => {

  it('a focused table WITHOUT split renders as a centered overlay while the Editor stays mounted', () => {
    appState.currentDocument = { document_id: 'd-1' };
    appState.currentTable = { table_id: 't-1' };
    mountPage();
    expect(document.querySelectorAll('[data-testid="table-focus"]').length).toBe(1);
    expect(document.querySelectorAll('[data-testid="editor"]').length).toBe(1);
    expect(document.querySelectorAll('[data-testid="split-layout"]').length).toBe(0);
    unmount();
  });
});
