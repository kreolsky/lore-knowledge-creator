/**
 * ProjectPage — minimal mount smoke (T12 tech-debt audit).
 *
 * pages/* had ZERO test files; ProjectPage is the recently-reworked authenticated
 * shell. Deep coverage is blocked by the broad composition (panels, editor,
 * collab context, many stores). This smoke stubs every child + the shared
 * ProjectShell so the page's OWN wiring (store reads, tab aggregation, event
 * effects) mounts without throwing — 0→smoke coverage.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let ProjectPage: typeof import('./ProjectPage').ProjectPage;

beforeEach(async () => {
  vi.resetModules();

  // Stub every composed child so only ProjectPage's own logic runs.
  const stub = () => null;
  const shellStub = (props: any) =>
    createElement('div', { 'data-testid': 'shell' }, props.renderCenter?.());
  vi.doMock('../components/ProjectShell', () => ({ ProjectShell: shellStub }));
  for (const c of [
    'Sidebar', 'TableOfContents', 'NotesPanel', 'ReferencesPanel', 'ChatPanel',
    'HistoryPanel', 'SearchPanel', 'LinksPanel', 'ProjectSettingsPanel', 'AccessPanel',
    'SnapshotModal', 'NoteConnector',
  ]) {
    vi.doMock(`../components/${c}`, () => ({ [c]: stub }));
  }
  vi.doMock('../components/editor/SelectionToolbar', () => ({ __esModule: true, default: stub }));
  vi.doMock('../components/editor/FindReplacePanel', () => ({ FindReplacePanel: stub }));
  vi.doMock('../components/editor/LinkSuggestionsPopup', () => ({ LinkSuggestionsPopup: stub }));
  vi.doMock('../components/editor/EditorLinkPreview', () => ({ EditorLinkPreview: stub }));

  vi.doMock('../hooks/useEvent', () => ({ useEvent: () => () => {} }));
  vi.doMock('../hooks/useAccessTabState', () => ({ useAccessTabState: () => ({ tint: null, active: false }) }));
  vi.doMock('../collab/ProjectCollabContext', () => ({ ProjectCollabContext: { Provider: ({ children }: any) => children } }));
  vi.doMock('../utils/right-panel-ready', () => ({ isRightPanelReady: () => true }));
  // Spread the real module: the load-failure path decides mirror deletion with the real
  // isAccessRefusal, and a bare stub leaves it undefined.
  vi.doMock('../api/client', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../api/client')>()),
    apiClient: { get: vi.fn().mockResolvedValue({}), post: vi.fn().mockResolvedValue({}) },
  }));
  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string) => k }),
    t: (k: string) => k,
  }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({
    FileText: icon, List: icon, MessageSquare: icon, Paperclip: icon, Archive: icon,
    MessagesSquare: icon, Search: icon, ArrowDownLeft: icon, Settings: icon,
    Replace: icon, ShieldCheck: icon,
  }));
  // Plan "public-document-ids": ProjectPage reads ids via useDocumentRoute(), not
  // useParams(). The bare /docs/:id route provides documentId only — projectId is
  // resolved from the store (currentProject). Mocking useParams WITHOUT projectId
  // (simulating /docs/:id) exercises that store-fallback so this smoke does not
  // stay green over a route that no longer carries projectId (false-green rule).
  vi.doMock('../hooks/useDocumentRoute', () => ({
    useDocumentRoute: () => ({ projectId: 'p1', documentId: 'doc-1' }),
  }));
  vi.doMock('react-router-dom', () => ({
    useParams: () => ({}),
    useNavigate: () => () => {},
    Outlet: () => createElement('div', { 'data-testid': 'outlet' }),
    useLocation: () => ({ pathname: '/docs/doc-1' }),
  }));

  const project = { project_id: 'p1', owner_id: 'u-me', name: 'Proj', index_doc_id: null };
  const mkStore = (state: Record<string, unknown>) => {
    const proxied = new Proxy(state, { get(t, k: string) { return k in t ? t[k] : () => {}; } });
    const s: any = (sel: (s: any) => any) => sel(proxied);
    s.subscribe = () => () => {};
    s.getState = () => proxied;
    s.getInitialState = () => proxied;
    return s;
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: mkStore({ currentProject: project, currentUser: { user_id: 'u-me' }, accessLevel: 'full' }),
  }));
  vi.doMock('../store/ui-store', () => ({
    useUIStore: mkStore({ rightTab: 'references', rightPanelWidth: 360 }),
    useDocState: () => ({ isReady: true }),
    // ProjectPage imports ORPHAN_KEY for the search-overlay doc-key comparison.
    ORPHAN_KEY: '__orphan__',
  }));
  vi.doMock('../store/note-store', () => ({ useNoteStore: mkStore({}) }));
  vi.doMock('../store/chat-store', () => ({ useChatStore: mkStore({}) }));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../components/ProjectShell');
  vi.doUnmock('../i18n');
  vi.doUnmock('lucide-react');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
  vi.doUnmock('../store/note-store');
  vi.doUnmock('../store/chat-store');
});

describe('ProjectPage smoke', () => {
  it('mounts without throwing and renders the shared shell', async () => {
    ProjectPage = (await import('./ProjectPage')).ProjectPage;
    act(() => root.render(createElement(ProjectPage)));
    expect(container.querySelector('[data-testid="shell"]')).toBeTruthy();
  });
});
