/**
 * Subtree chat privacy — the doc-tree half: the row carries a green mark while
 * `chats_unindexed` is set, at every access level; the toggle is NOT in the
 * tree (it lives in the Access tab, access/ChatsPrivacySection).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import type { Document, DocumentTreeNode } from '../types';

let container: HTMLDivElement;
let root: Root;
let DocumentTree: typeof import('./DocumentTree').DocumentTree;
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let setDocuments: ReturnType<typeof vi.fn>;
let initialDocs: Document[];

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

/** doc-1 plain, doc-2 flagged — the flat list mirrors the tree so the
 * optimistic handler's map over `documents` has rows to patch. */
function makeDocs(): { flat: Document[]; tree: DocumentTreeNode[] } {
  const plain = makeNode('doc-1', 'Doc 1');
  const flagged = makeNode('doc-2', 'Doc 2', { chats_unindexed: true });
  return { flat: [plain, flagged], tree: [plain, flagged] };
}

beforeEach(async () => {
  vi.resetModules();
  patchMock = vi.fn();
  showToast = vi.fn();
  setDocuments = vi.fn();
  initialDocs = makeDocs().flat;

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
    documentTree: makeDocs().tree,
    currentReference: null,
    currentDocument: null,
    setCurrentDocument: vi.fn(),
    documents: initialDocs,
    setDocuments,
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

function renderTree(canEdit = true) {
  act(() => {
    root.render(createElement(DocumentTree, {
      onDelete: () => {},
      onCreateChild: () => {},
      onChangeParent: () => {},
      canEdit,
    }));
  });
}

describe('DocumentTree subtree chat privacy — row mark', () => {

  it('an editor tree has no privacy toggle in the gear — the Access tab owns it', () => {
    renderTree(true);
    expect(container.querySelector('[data-doc-id="doc-2"] [data-chats-private-mark]')).not.toBeNull();
    expect(container.querySelector('[data-chats-private-toggle]')).toBeNull();
  });
});
