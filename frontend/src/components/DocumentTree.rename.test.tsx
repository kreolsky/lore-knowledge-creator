/**
 * Dead-button regression: a FAILED document rename
 * must surface an error toast. The catch used to only console.error — the row
 * kept its edit input open with no explanation of why nothing applied.
 * Pinned behavior: toast fires AND the edit input STAYS open (deferred-close
 * rule — the user's text is preserved for a retry, the toast says why).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import type { DocumentTreeNode } from '../types';

let container: HTMLDivElement;
let root: Root;
let DocumentTree: typeof import('./DocumentTree').DocumentTree;
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;

const makeNode = (id: string, title: string): DocumentTreeNode => ({
  document_id: id,
  project_id: 'p',
  parent_id: null,
  title,
  content: '',
  path: '',
  is_index: false,
  children: [],
  created_at: '',
  updated_at: '',
});

beforeEach(async () => {
  vi.resetModules();
  patchMock = vi.fn();
  showToast = vi.fn();

  vi.doMock('../hooks/useDocumentPreview', () => ({ useDocumentPreview: () => ({ content: undefined, error: false, loading: false }) }));
  vi.doMock('../hooks/useHoverPreview', () => ({
    useHoverPreview: () => ({ handleHover: vi.fn(), handleHoverLeave: vi.fn() }),
  }));
  vi.doMock('../hooks/useSiblingDragReorder', () => ({ useSiblingDragReorder: () => {}, treeDragAdapter: {} }));
  vi.doMock('./HoverPreviewPopup', () => ({ HoverPreviewPopup: () => null }));
  vi.doMock('./ui', () => ({ Button: () => null }));
  vi.doMock('react-router-dom', () => ({ useParams: () => ({ documentId: 'doc-1' }) }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockResolvedValue({}), patch: patchMock, post: vi.fn(), delete: vi.fn() },
  }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  const appState = {
    currentProject: { index_doc_id: null, project_id: 'p' },
    documentTree: [makeNode('doc-1', 'Doc 1')],
    currentReference: null,
    currentDocument: null,
    setCurrentDocument: vi.fn(),
    documents: [],
    setDocuments: vi.fn(),
    referenceSourceDocId: null,
    snapshotPreview: null,
    accessLevel: 'full',
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));

  const uiState = {
    collapsedDocIds: [],
    toggleDocExpanded: vi.fn(),
    expandDocs: vi.fn(),
    setSidebarTab: vi.fn(),
    isPublicShare: false,
    documents: {},
  };
  vi.doMock('../store/ui-store', () => ({
    useUIStore: Object.assign(
      (selector: (s: typeof uiState) => unknown) => selector(uiState),
      { getState: () => uiState },
    ),
  }));

  ({ DocumentTree } = await import('./DocumentTree'));

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
  vi.doUnmock('../store/ui-store');
});

/** Open the row's rename input via double-click, type a new title, press Enter. */
async function renameDocTo(newTitle: string) {
  act(() => {
    root.render(createElement(DocumentTree, {
      onDelete: () => {},
      onCreateChild: () => {},
      onChangeParent: () => {},
      canEdit: true,
    }));
  });

  const label = container.querySelector('.doc-label');
  if (!label) throw new Error('doc label not rendered');
  act(() => { label.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });

  const input = container.querySelector<HTMLInputElement>('[data-rename-input]');
  if (!input) throw new Error('rename input not rendered after dblclick');
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    nativeSetter.call(input, newTitle);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });

  await act(async () => {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  });
}

describe('DocumentTree rename — failure surfaces a toast (no dead button)', () => {
  it('a rejecting PATCH /documents shows the renameDocumentFailed error toast and keeps the edit input open', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    await renameDocTo('Renamed');

    expect(showToast).toHaveBeenCalledWith('renameDocumentFailed', 'error');
    expect(container.querySelector('[data-rename-input]'), 'edit input must stay open (deferred close; toast explains)').not.toBeNull();
  });
});
