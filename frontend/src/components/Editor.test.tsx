/**
 * Editor — minimal mount smoke (T12 tech-debt audit).
 *
 * Editor.tsx is one of the most coupled surfaces (CM6 + collab + autosave +
 * position cache + many editor/* extensions) and had NO component coverage.
 * Deep tests stay blocked by the Editor.tsx:16 coupling DEBT. This is the
 * pragmatic smoke tier: it mounts the Editor in its empty state (no active
 * document/reference) and asserts the placeholder renders without throwing —
 * exercising the store reads, useDeferredValue, refs, and the extension-building
 * useMemo, while stubbing the heavy CM6 wrapper + collab hooks so no real
 * editor/collab is instantiated.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let Editor: typeof import('./Editor').Editor;

beforeEach(async () => {
  vi.resetModules();

  // Stub the CM6 wrapper + banner/media children so their import trees (and any
  // real CM6/collab instantiation) do not run. The extension-building modules
  // still import, but CodeMirrorEditor never mounts in the empty state.
  vi.doMock('./editor/CodeMirrorEditor', () => ({
    __esModule: true,
    default: () => createElement('div', { 'data-testid': 'cm-stub' }),
    cmSetup: () => [], // stub the CM6 setup Extension builder
  }));
  const stub = () => null;
  vi.doMock('./editor/SnapshotPreviewBanner', () => ({ SnapshotPreviewBanner: stub }));
  vi.doMock('./editor/ReferenceViewerBanner', () => ({ ReferenceViewerBanner: stub }));
  vi.doMock('./editor/PreviewDocumentBanner', () => ({ PreviewDocumentBanner: stub }));
  vi.doMock('./editor/CollabPresenceChips', () => ({ CollabPresenceChips: stub }));
  vi.doMock('./editor/ReferenceMediaBar', () => ({ ReferenceMediaBar: stub }));
  vi.doMock('./editor/dev-table-test-hook', () => ({ mountTableTestHook: () => () => {} }));

  vi.doMock('../hooks/useEditorAutosave', () => ({ useEditorAutosave: () => ({}) }));
  vi.doMock('../hooks/useEditorPosition', () => ({ useEditorPosition: () => ({}) }));

  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string) => k }),
    t: (k: string) => k,
  }));
  vi.doMock('../events', () => ({ emit: vi.fn(), on: () => () => {}, off: () => {} }));
  vi.doMock('react-router-dom', () => ({
    useNavigate: () => () => {},
    useParams: () => ({}),
    useLocation: () => ({ pathname: '/' }),
  }));

  // Empty state: no active document / reference / preview → activeItem is undefined.
  const baseStore: Record<string, unknown> = {
    currentDocument: null,
    currentReference: null,
    currentProject: null,
    currentUser: null,
    previewDocument: null,
    snapshotPreview: null,
    accessLevel: 'full',
    documents: [],
    references: [],
    referenceSourceDocId: null,
    previewScrollOffset: null,
  };
  // Proxy: any unread field defaults to [] (safe to iterate) for collections, or
  // a no-op fn when called — keeps the heavily-coupled Editor's many store reads
  // from one-by-one breaking the smoke without over-specifying the state shape.
  const emptyStore = new Proxy(baseStore, {
    get(target, key: string) {
      if (key in target) return target[key];
      return () => {};
    },
  });
  // Zustand store shape: a callable hook (selector) + the static store methods
  // some non-stubbed deps reach for (.subscribe / .getState / .getInitialState).
  const useAppStore: any = (selector: (s: any) => any) => selector(emptyStore);
  useAppStore.subscribe = () => () => {};
  useAppStore.getState = () => emptyStore;
  useAppStore.getInitialState = () => emptyStore;
  vi.doMock('../store/app-store', () => ({ useAppStore }));
  vi.doMock('../store/ui-store', () => ({
    useUIStore: (selector?: (s: any) => any) =>
      selector ? selector({ documentPositions: {} }) : { documentPositions: {} },
  }));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of [
    './editor/CodeMirrorEditor', './editor/SnapshotPreviewBanner', './editor/ReferenceViewerBanner',
    './editor/PreviewDocumentBanner', './editor/CollabPresenceChips', './editor/ReferenceMediaBar',
    './editor/dev-table-test-hook', '../hooks/useEditorAutosave',
    '../hooks/useEditorPosition', '../i18n', '../events', 'react-router-dom',
    '../store/app-store', '../store/ui-store',
  ]) {
    vi.doUnmock(m);
  }
});

describe('Editor smoke', () => {
  // Generous timeout: this smoke dynamically imports the full CM6 + ~20 editor/*
  // modules (the Editor.tsx coupling DEBT blocks a lighter test). That load takes
  // 1.5–4s+ and varies with runner load; the default 5s timeout flakes. This is
  // heavy-but-legitimate work, not a hang.
  it('mounts without throwing in the empty state (no active doc/ref)', { timeout: 30_000 }, async () => {
    Editor = (await import('./Editor')).Editor;
    act(() => root.render(createElement(Editor)));
    // The placeholder copy renders — mount succeeded, no exception.
    expect(container.textContent).toContain('selectDocOrRef');
  });
});
