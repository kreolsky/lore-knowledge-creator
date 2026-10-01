/**
 * Reveal-in-tree handler wiring: emitting 'reveal-in-tree' must expand the
 * focused document's collapsed ancestors and switch the sidebar to the docs tab.
 *
 * Asserted through the store (collapsedDocIds / sidebarTab), NOT through the DOM.
 * Uses the REAL ui-store + REAL event bus; only app-store, heavy hooks, router,
 * api, and i18n are stubbed.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

import type { DocumentTreeNode } from '../types';

const makeNode = (id: string, parent: string | null, children: DocumentTreeNode[] = []): DocumentTreeNode => ({
  document_id: id,
  project_id: 'p',
  parent_id: parent,
  title: id,
  content: '',
  path: '',
  is_index: false,
  children,
  created_at: '',
  updated_at: '',
});

let container: HTMLDivElement;
let root: Root;
let DocumentTree: typeof import('./DocumentTree').DocumentTree;
let useUIStore: typeof import('../store/ui-store').useUIStore;
let emit: typeof import('../events').emit;

function Harness() {
  return createElement(DocumentTree, {
    onDelete: () => {},
    onCreateChild: () => {},
    onChangeParent: () => {},
    canEdit: false,
  });
}

beforeEach(async () => {
  vi.resetModules();

  vi.doMock('../hooks/useDocumentPreview', () => ({ useDocumentPreview: () => ({ content: undefined, error: false, loading: false }) }));
  vi.doMock('../hooks/useHoverPreview', () => ({ useHoverPreview: () => ({ handleHover: vi.fn(), handleHoverLeave: vi.fn() }) }));
  vi.doMock('../hooks/useSiblingDragReorder', () => ({ useSiblingDragReorder: () => {}, treeDragAdapter: {} }));
  vi.doMock('./HoverPreviewPopup', () => ({ HoverPreviewPopup: () => null }));
  vi.doMock('./ui', () => ({ Button: () => null }));
  vi.doMock('react-router-dom', () => ({ useParams: () => ({}) }));

  // `put` must exist and resolve: the reveal flow schedules ui-store's debounced
  // preferences save (200 ms), which fires AFTER the tests finish — a mock without
  // `put` turns that late timer into an unhandled TypeError.
  vi.doMock('../api/client', () => ({ apiClient: { get: vi.fn(), put: vi.fn(() => Promise.resolve({})) } }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  // documentTree: root → mid → leaf; documents: the same flat chain.
  const leaf = makeNode('leaf', 'mid');
  const mid = makeNode('mid', 'root', [leaf]);
  const treeRoot = makeNode('root', null, [mid]);
  const flat = [
    { document_id: 'root', parent_id: null },
    { document_id: 'mid', parent_id: 'root' },
    { document_id: 'leaf', parent_id: 'mid' },
  ];

  vi.doMock('../store/app-store', () => {
    const appState = {
      currentProject: { index_doc_id: null, project_id: 'p' },
      documentTree: [treeRoot],
      currentReference: null,
      currentDocument: flat[2],
      documents: flat,
      setDocuments: () => {},
      setCurrentDocument: () => {},
      referenceSourceDocId: null,
      snapshotPreview: null,
    };
    // zustand-shaped: both `useAppStore(selector)` and `useAppStore.getState()` resolve
    // to the same state (the reveal handler reads documents via getState at event time).
    const useAppStore = Object.assign(
      (selector: (s: any) => any) => selector(appState),
      { getState: () => appState },
    );
    return { useAppStore };
  });

  // REAL ui-store: hydrate before DocumentTree mounts so its selectors resolve.
  const ui = await import('../store/ui-store');
  useUIStore = ui.useUIStore;
  ui.registerAppBridge({
    getAppContext: () => ({ currentUser: { user_id: 'u1' }, currentProject: { project_id: 'p1' }, currentDocument: null }),
    showToast: () => {},
  });
  ui.clearLastSavedBlobs();
  useUIStore.setState({ documents: {}, projectPrefsLoaded: true, collapsedDocIds: ['root', 'mid'], sidebarTab: 'toc' });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({}) } as Response);

  const mod = await import('./DocumentTree');
  DocumentTree = mod.DocumentTree;
  const ev = await import('../events');
  emit = ev.emit;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../hooks/useDocumentPreview');
  vi.doUnmock('../hooks/useHoverPreview');
  vi.doUnmock('../hooks/useSiblingDragReorder');
  vi.doUnmock('./HoverPreviewPopup');
  vi.doUnmock('./ui');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.restoreAllMocks();
});

describe('reveal-in-tree handler — expands ancestors + switches to docs tab', () => {
  it('emitting reveal-in-tree removes collapsed ancestors and sets the docs tab', () => {
    act(() => root.render(createElement(Harness)));

    emit('reveal-in-tree', { documentId: 'leaf' });

    // leaf's ancestors are root, mid — both collapsed. Reveal only expands.
    expect(useUIStore.getState().collapsedDocIds).toEqual([]);
    expect(useUIStore.getState().sidebarTab).toBe('docs');
  });

  it('does not collapse an already-expanded sibling (only-expands invariant)', () => {
    // sibling 'other' is expanded (not in collapsedDocIds); reveal must not add it.
    useUIStore.setState({ collapsedDocIds: ['mid', 'unrelated-keep'] });
    act(() => root.render(createElement(Harness)));

    emit('reveal-in-tree', { documentId: 'leaf' });

    // root was already expanded (absent); mid removed; unrelated-keep untouched.
    expect(useUIStore.getState().collapsedDocIds).toEqual(['unrelated-keep']);
  });
});
