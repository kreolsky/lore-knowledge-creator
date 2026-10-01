/**
 * The sidebar tree must not re-render on a `setDocuments` that changes nothing.
 *
 * This is the render-side half of the create/delete flash. The store half
 * (`setDocuments` preserving identities) is pinned in
 * store/app-store/document-tree-slice.identity.test.ts; here the REAL store is
 * wired to the REAL tree so the subscription itself is under test — a row that
 * subscribes to the flat `documents` array by reference re-renders on every
 * call, `memo` notwithstanding.
 *
 * Only the peripheral hooks are mocked (preview, drag, i18n, router, ui-store):
 * mocking the app-store would mock away the very thing being measured.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, Profiler, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { Document } from '../types';

// React only batches through act() when the environment opts in; without this the
// commit counts below would be measured outside React's control.
(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let commits: number;
let DocumentTree: typeof import('./DocumentTree').DocumentTree;
let useAppStore: typeof import('../store/app-store').useAppStore;

const makeDoc = (id: string, title: string, sortKey: string): Document => ({
  document_id: id,
  project_id: 'p',
  parent_id: null,
  title,
  content: '',
  path: '',
  is_index: false,
  sort_key: sortKey,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
});

const DOCS = [makeDoc('d1', 'One', 'a0'), makeDoc('d2', 'Two', 'a1')];

function Harness() {
  return createElement(
    Profiler,
    { id: 'tree', onRender: () => { commits += 1; } },
    createElement(DocumentTree, {
      onDelete: () => {},
      onCreateChild: () => {},
      onChangeParent: () => {},
      canEdit: false,
    }),
  );
}

beforeEach(async () => {
  vi.resetModules();
  commits = 0;

  vi.doMock('../hooks/useDocumentPreview', () => ({
    useDocumentPreview: () => ({ content: undefined, error: false, loading: false }),
  }));
  vi.doMock('../hooks/useHoverPreview', () => ({
    useHoverPreview: () => ({ handleHover: vi.fn(), handleHoverLeave: vi.fn() }),
  }));
  vi.doMock('../hooks/useSiblingDragReorder', () => ({ useSiblingDragReorder: () => {}, treeDragAdapter: {} }));
  vi.doMock('../hooks/useDocumentRoute', () => ({ useDocumentRoute: () => ({ documentId: 'd1' }) }));
  vi.doMock('./HoverPreviewPopup', () => ({ HoverPreviewPopup: () => null }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  // Partial mock: app-store imports registerAppBridge from here at module load.
  // Only `useUIStore` is replaced — with a STATIC state, so UI-store churn can
  // never be mistaken for the app-store re-render this spec measures.
  vi.doMock('../store/ui-store', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../store/ui-store')>()),
    useUIStore: (selector?: (s: any) => any) => {
      const state = {
        collapsedDocIds: [],
        toggleDocExpanded: () => {},
        expandDocs: () => {},
        setSidebarTab: () => {},
        isPublicShare: false,
        documents: {},
      };
      return selector ? selector(state) : state;
    },
  }));

  ({ useAppStore } = await import('../store/app-store'));
  ({ DocumentTree } = await import('./DocumentTree'));

  useAppStore.setState({ currentProject: null, currentReference: null, snapshotPreview: null });
  useAppStore.getState().setDocuments(DOCS.map((d) => ({ ...d })));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(createElement(Harness)); });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe('DocumentTree re-render on setDocuments', () => {
  it('renders a row per document', () => {
    expect(container.querySelectorAll('[data-doc-id]')).toHaveLength(2);
  });

  it('does not re-render when a fresh array carries identical content', async () => {
    const baseline = commits;
    expect(baseline).toBeGreaterThan(0);

    await act(async () => {
      useAppStore.getState().setDocuments(DOCS.map((d) => ({ ...d })));
    });

    expect(commits).toBe(baseline);
  });

  it('does not re-render when an unrelated document field changes', async () => {
    // A poll bringing a new `updated_at` for one doc must not repaint the tree:
    // the flat list changes, but no tree-visible field does.
    const baseline = commits;

    await act(async () => {
      useAppStore.getState().setDocuments(
        DOCS.map((d) =>
          d.document_id === 'd2' ? { ...d, updated_at: '2026-08-17T12:00:00Z' } : { ...d },
        ),
      );
    });

    expect(commits).toBe(baseline);
  });

  it('still re-renders when a title changes', async () => {
    const baseline = commits;

    await act(async () => {
      useAppStore.getState().setDocuments(
        DOCS.map((d) => (d.document_id === 'd2' ? { ...d, title: 'Renamed' } : { ...d })),
      );
    });

    expect(commits).toBeGreaterThan(baseline);
    expect(container.textContent).toContain('Renamed');
  });
});
