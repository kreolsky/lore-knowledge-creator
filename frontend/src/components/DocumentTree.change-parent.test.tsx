/**
 * DocumentTree — a system document offers no "change parent" action: the server
 * refuses to move it (403), so the picker would only lead to a failure toast.
 * Pinned in both directions: a plain document still offers the action.
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

let tree: DocumentTreeNode[];

const makeNode = (id: string, title: string, extra: Partial<DocumentTreeNode> = {}): DocumentTreeNode => ({
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
  ...extra,
});

beforeEach(async () => {
  vi.resetModules();
  tree = [];
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
    get documentTree() { return tree; },
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

function render() {
  act(() => {
    root.render(createElement(DocumentTree, {
      onDelete: () => {},
      onCreateChild: () => {},
      onChangeParent: () => {},
      canEdit: true,
    }));
  });
}

const changeParentButtons = () => container.querySelectorAll('button[title="changeParent"]');

describe('DocumentTree change-parent action', () => {
  it('is offered on a plain document', () => {
    tree = [makeNode('doc-1', 'Doc 1')];
    render();
    expect(changeParentButtons()).toHaveLength(1);
  });

  it('is hidden on a system document', () => {
    tree = [makeNode('sys-1', 'Personas', { is_system: true })];
    render();
    expect(container.querySelector('.doc-label')).not.toBeNull();
    expect(changeParentButtons()).toHaveLength(0);
  });
});
