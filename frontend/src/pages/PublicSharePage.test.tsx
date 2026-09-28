/**
 * PublicSharePage — minimal mount smoke (T12 tech-debt audit).
 *
 * The public /s/:token surface (recently reworked to reuse the authed ProjectShell)
 * had no page coverage. This smoke stubs the children + shell + stores/router and
 * mocks the doc fetch so the page mounts into its ready state without throwing.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let PublicSharePage: typeof import('./PublicSharePage').PublicSharePage;

beforeEach(async () => {
  vi.resetModules();

  const stub = () => null;
  const shellStub = (props: any) =>
    createElement('div', { 'data-testid': 'shell' }, props.renderCenter?.());
  vi.doMock('../components/ProjectShell', () => ({ ProjectShell: shellStub }));
  vi.doMock('../components/PublicEditor', () => ({ PublicEditor: () => createElement('div', { 'data-testid': 'public-editor' }) }));
  vi.doMock('../components/editor/EditorLinkPreview', () => ({ EditorLinkPreview: stub }));
  vi.doMock('../components/Sidebar', () => ({ Sidebar: stub }));
  vi.doMock('../components/TableOfContents', () => ({ TableOfContents: stub }));
  vi.doMock('../components/ReferencesPanel', () => ({ ReferencesPanel: stub }));

  vi.doMock('../hooks/useEvent', () => ({ useEvent: () => () => {} }));
  vi.doMock('../utils/reference-url', () => ({ setPublicFileContext: vi.fn() }));
  vi.doMock('../utils/routing', () => ({ docUrl: (id: string) => `/docs/${id}` }));
  // plan "public-document-ids": PublicSharePage is keyed on the document uuid
  // (document-keyed anonymous endpoints), not a share token.
  vi.doMock('../api/public-share', () => ({
    publicTreeByDoc: vi.fn().mockResolvedValue({ scope: 'doc', project_name: 'Proj', root_id: 'doc-1', nodes: [] }),
    publicDocumentByDoc: vi.fn().mockResolvedValue({ document_id: 'doc-1', title: 'Shared', content: '# hi', tables_json: null, headings: [] }),
    publicReferencesByDoc: vi.fn().mockResolvedValue([]),
  }));
  // Stable `t`: the real useTranslation returns a referentially-stable t; a mock
  // that builds a new fn each render destabilizes effect deps and loops the
  // hydrate effect (loading↔ready). Mirror the real contract.
  const stableT = (k: string) => k;
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: stableT }) }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({ AlertCircle: icon, FileText: icon, List: icon, Paperclip: icon }));
  vi.doMock('react-router-dom', () => ({ useNavigate: () => () => {} }));

  // The page fetches the shared doc via apiClient.get; stub it so it resolves
  // into the ready phase instead of hitting jsdom's relative-URL fetch.
  vi.doMock('../api/client', () => ({
    apiClient: {
      get: vi.fn().mockResolvedValue({ document_id: 'doc-1', title: 'Shared', content: '# hi', tables_json: null }),
    },
  }));

  // WHY one stable noop: the Proxy's get-trap is hit on every selector call
  // (useAppStore(s => s.setDocuments)); returning a NEW fn each time makes
  // effect deps unstable and the hydrate effect re-run every render. Real
  // Zustand setters are referentially stable — mirror that with one shared fn.
  const noop = () => {};
  const mkStore = (state: Record<string, unknown>) => {
    const proxied = new Proxy(state, { get(t, k: string) { return k in t ? t[k] : noop; } });
    const s: any = (sel: (s: any) => any) => sel(proxied);
    s.subscribe = () => () => {};
    s.getState = () => proxied;
    s.getInitialState = () => proxied;
    return s;
  };
  vi.doMock('../store/app-store', () => ({ useAppStore: mkStore({ documents: [] }) }));
  vi.doMock('../store/ui-store', () => ({ useUIStore: mkStore({}) }));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../components/ProjectShell');
  vi.doUnmock('../components/PublicEditor');
  vi.doUnmock('../i18n');
  vi.doUnmock('lucide-react');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

describe('PublicSharePage smoke', () => {
  it('mounts without throwing and renders the shared shell once the doc resolves', async () => {
    PublicSharePage = (await import('./PublicSharePage')).PublicSharePage;
    act(() => root.render(createElement(PublicSharePage, { documentId: 'doc-1' })));
    // The fetch effect resolves asynchronously; flush it so the page leaves the
    // loading phase and renders the shell.
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(container.querySelector('[data-testid="shell"]')).toBeTruthy();
  });

  it('downgrades accessLevel to readonly on mount and RESTORES it on unmount', async () => {
    // The authed-fallback mount (EditorRoutes serves this page to a signed-in
    // visitor whose read was refused) must not leave a stale 'readonly' behind
    // when the visitor navigates back to authed surfaces.
    const calls: string[] = [];
    const state: Record<string, unknown> = {
      documents: [],
      accessLevel: 'full',
      setAccessLevel: (level: string) => { state.accessLevel = level; calls.push(level); },
    };
    const store: any = (sel: (s: any) => any) => sel(state);
    store.getState = () => state;
    // Overrides the beforeEach mkStore mock: the page import below is per-test
    // (vi.resetModules), so the LATEST doMock registration is what it resolves.
    vi.doMock('../store/app-store', () => ({ useAppStore: store }));

    PublicSharePage = (await import('./PublicSharePage')).PublicSharePage;
    act(() => root.render(createElement(PublicSharePage, { documentId: 'doc-1' })));
    expect(calls).toEqual(['readonly']);

    act(() => root.unmount());
    expect(calls).toEqual(['readonly', 'full']);
  });
});
