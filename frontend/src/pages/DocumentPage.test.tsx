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
  it('commits doc+refs+restoredReference together; cached client content wins over the server copy', async () => {
    const refs = [{ reference_id: 'r-1', title: 'R1', document_id: 'd-1', media_type: 'text' }];
    loadReferences.mockResolvedValue(refs);
    uiGetState.getCurrentReferenceForDoc = vi.fn(() => 'r-1');
    appState.documents = [{ document_id: 'd-1', content: 'client-fresher', headings: ['h'] }];
    const host = mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(apiGet).toHaveBeenCalledWith('/documents/d-1?track=1');
    expect(appState.setCurrentDocument).toHaveBeenCalledTimes(1);
    const [committed, opts] = (appState.setCurrentDocument as ReturnType<typeof vi.fn>).mock.calls[0];
    // Cache-fresher INVARIANT (DocumentPage.tsx) — client content may be fresher
    // than the server during the WS flush delay.
    expect(committed.content).toBe('client-fresher');
    expect(committed.updated_at).toBe(SERVER_DOC.updated_at);
    expect(opts.references).toEqual(refs);
    expect(opts.restoredReference?.reference_id).toBe('r-1');
    unmount();
  });

  it('skips the fetch when the document is already the committed currentDocument (StrictMode remount)', () => {
    appState.currentDocument = { document_id: 'd-1' };
    mountPage();
    expect(apiGet).not.toHaveBeenCalled();
    unmount();
  });

  it('doc fetch failure bounces to the project (or landing when no project)', async () => {
    apiGet.mockRejectedValue(new Error('boom'));
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(navigate).toHaveBeenCalledWith('/projects/p1');
    unmount();

    navigate.mockClear();
    routeState.projectId = undefined;
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(navigate).toHaveBeenCalledWith('/');
    unmount();
  });

  it('a refused per-id fetch falls back to the public view, not a bounce', async () => {
    const { ForbiddenError } = await import('../api/client');
    apiGet.mockRejectedValue(new ForbiddenError());
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(uiGetState.markPublicFallback).toHaveBeenCalledWith(['d-1']);
    expect(navigate).not.toHaveBeenCalled();
    unmount();
  });

  it('staleness guard: a DIFFERENT active project means no commit', async () => {
    appState.currentProject = { project_id: 'p-OTHER' };
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(appState.setCurrentDocument).not.toHaveBeenCalled();
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

  it('one-shot /open bundle seeds project + access level + note sessions, then commits', async () => {
    const bundle = {
      document: { ...SERVER_DOC, content: 'bundle' },
      references: [{ reference_id: 'r-b', title: 'RB', document_id: 'd-1', media_type: 'text' }],
      project: { project_id: 'p9', my_access: 'commentator' },
      note_sessions: [{ session_id: 'n-1' }],
    };
    apiGet.mockResolvedValue(bundle);
    mountPage();
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(apiGet).toHaveBeenCalledWith('/documents/open/d-1');
    expect(apiGet).toHaveBeenCalledTimes(1); // ONE round trip — no per-id re-fetch
    expect(appState.setCurrentProject).toHaveBeenCalledWith(bundle.project);
    // Access-level INVARIANT (DocumentPage.tsx) — set from the SAME payload as the
    // project; the store default is 'full'.
    expect(appState.setAccessLevel).toHaveBeenCalledWith('commentator');
    expect(noteChatGetState.hydrateSessions).toHaveBeenCalledWith('p9', 'd-1', bundle.note_sessions);
    const [committed, opts] = (appState.setCurrentDocument as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(committed.content).toBe('bundle');
    expect(opts.references).toEqual(bundle.references);
    unmount();
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
  it('split needs doc + (ref|table) and no preview/snapshot; table routes to the table split', () => {
    docStates['d-1'] = { refOpenMode: 'split' };
    appState.currentDocument = { document_id: 'd-1' };
    appState.currentReference = { reference_id: 'r-1' };
    let host = mountPage();
    expect(document.querySelectorAll('[data-testid="split-layout"]').length).toBe(1);
    expect((splitProps[0] as Record<string, unknown>).reference).toBeTruthy();
    unmount();

    appState.currentReference = null;
    appState.currentTable = { table_id: 't-1' };
    host = mountPage();
    expect((splitProps[1] as Record<string, unknown>).table).toBeTruthy();
    unmount();

    appState.previewDocument = { document_id: 'd-other' };
    mountPage();
    // Preview owns the full center — no split even with ref+splitView.
    expect(document.querySelectorAll('[data-testid="split-layout"]').length + document.querySelectorAll('[data-testid="table-focus"]').length).toBe(0);
    expect(document.querySelectorAll('[data-testid="editor"]').length).toBeGreaterThan(0);
    unmount();
  });

  it("'panel' mode + reference pins the center to the document — no split layout", () => {
    docStates['d-1'] = { refOpenMode: 'panel' };
    appState.currentDocument = { document_id: 'd-1' };
    appState.currentReference = { reference_id: 'r-1' };
    editorProps.length = 0;
    mountPage();
    expect(document.querySelectorAll('[data-testid="split-layout"]').length).toBe(0);
    expect(document.querySelectorAll('[data-testid="editor"]').length).toBe(1);
    expect((editorProps[0] as { entity?: unknown }).entity).toBe(appState.currentDocument);
    unmount();
  });

  it("a legacy persisted {splitView: true} still renders the split layout", () => {
    docStates['d-1'] = { splitView: true };
    appState.currentDocument = { document_id: 'd-1' };
    appState.currentReference = { reference_id: 'r-1' };
    mountPage();
    expect(document.querySelectorAll('[data-testid="split-layout"]').length).toBe(1);
    unmount();
  });

  it("'panel' mode + focused table renders the centered table overlay (tables ignore panel mode)", () => {
    docStates['d-1'] = { refOpenMode: 'panel' };
    appState.currentDocument = { document_id: 'd-1' };
    appState.currentTable = { table_id: 't-1' };
    mountPage();
    expect(document.querySelectorAll('[data-testid="table-focus"]').length).toBe(1);
    expect(document.querySelectorAll('[data-testid="split-layout"]').length).toBe(0);
    unmount();
  });

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

describe('DocumentPage — error-note events', () => {
  it('ws:extraction_error for the OPEN doc reloads sessions and opens the pinned note thread', async () => {
    appState.currentDocument = { document_id: 'd-1' };
    mountPage();
    expect(eventHandlers['ws:extraction_error']).toBeTruthy();
    await act(async () => {
      eventHandlers['ws:extraction_error']({ reference_id: 'r1', note_id: 'n-err', document_id: 'd-1' });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(noteChatGetState.loadSessions).toHaveBeenCalledWith('p1', 'd-1');
    expect(noteStoreState.setActiveNoteThreadId).toHaveBeenCalledWith('n-err', 'd-1');
    expect(noteChatGetState.setActiveSession).toHaveBeenCalledWith('n-err');
    unmount();
  });

  it('the same event for a DIFFERENT document is ignored', async () => {
    appState.currentDocument = { document_id: 'd-1' };
    mountPage();
    await act(async () => {
      eventHandlers['ws:extraction_error']({ reference_id: 'r1', note_id: 'n-x', document_id: 'd-OTHER' });
      await Promise.resolve();
    });
    expect(noteChatGetState.loadSessions).not.toHaveBeenCalled();
    unmount();
  });
});
