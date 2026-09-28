/**
 * Regression test for the public-share hover-preview path in DocumentTree.
 *
 * HISTORY: an earlier revision gated hover-preview entirely on isPublicShare
 * (handleDocHover early-returned) because useDocumentPreview hit the authed
 * GET /documents/{id}, which 401s anonymous callers and apiClient redirects
 * to '/' (kicks the visitor off /s/:token).
 *
 * CURRENT contract: `fetchDocumentContent` is now public-aware — when a public
 * token is set via `setPublicFileContext`, it routes through the anonymous
 * /public/{token}/documents/{id} endpoint. The DocumentTree gate is REMOVED;
 * hover-preview now works on /s/:token (parity with the authed surface).
 *
 * This test pins BOTH directions: hovering a doc on the authed surface AND on
 * the public surface must feed the docId through to useDocumentPreview. The
 * public-path fetch routing is covered by useDocumentPreview.test.ts.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

import type { DocumentTreeNode } from '../types';

// Module-level store snapshot — the doMock factory reads it at render time so
// each test can flip isPublicShare before rendering.
let __isPublicShare = false;

// Per-doc UI state handed to the mocked ui-store selector (refOpenMode etc.).
let __docUi: Record<string, { refOpenMode?: string }>;

// Hoisted so a test can mutate `documentTree` (e.g. inject a public_share node)
// before render; the doMock factory references this same object.
let appState: any;

let container: HTMLDivElement;
let root: Root;
let previewMock: ReturnType<typeof vi.fn>;
let DocumentTree: typeof import('./DocumentTree').DocumentTree;

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
  __isPublicShare = false;
  __docUi = {};

  // Spy: records every docId DocumentTree feeds to useDocumentPreview. The gate
  // is correct iff this is never called with a real id on the public surface.
  previewMock = vi.fn(() => ({ content: undefined, error: false, loading: false }));

  vi.doMock('../hooks/useDocumentPreview', () => ({ useDocumentPreview: previewMock }));
  vi.doMock('../hooks/useHoverPreview', () => ({
    useHoverPreview: () => ({ handleHover: vi.fn(), handleHoverLeave: vi.fn() }),
  }));
  vi.doMock('../hooks/useTreeDragReorder', () => ({ useTreeDragReorder: () => {} }));
  vi.doMock('./HoverPreviewPopup', () => ({ HoverPreviewPopup: () => null }));
  vi.doMock('./ui', () => ({ Button: () => null }));
  vi.doMock('react-router-dom', () => ({ useParams: () => ({ documentId: 'doc-1' }) }));

  vi.doMock('../api/client', () => ({ apiClient: { get: vi.fn() } }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  const appStateLocal = {
    currentProject: { index_doc_id: null, project_id: 'p' },
    documentTree: [makeNode('doc-1', 'Doc 1')],
    currentReference: null,
    setCurrentDocument: () => {},
    documents: [],
    setDocuments: () => {},
    referenceSourceDocId: null,
    snapshotPreview: null,
  };
  appState = appStateLocal;

  vi.doMock('../store/app-store', () => ({
    useAppStore: (selector: (s: any) => any) => selector(appStateLocal),
  }));

  vi.doMock('../store/ui-store', () => ({
    useUIStore: (selector?: (s: any) => any) =>
      selector
        ? selector({ collapsedDocIds: [], toggleDocExpanded: () => {}, isPublicShare: __isPublicShare, documents: __docUi })
        : { isPublicShare: __isPublicShare },
  }));

  const mod = await import('./DocumentTree');
  DocumentTree = mod.DocumentTree;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../hooks/useDocumentPreview');
  vi.doUnmock('../hooks/useHoverPreview');
  vi.doUnmock('../hooks/useTreeDragReorder');
  vi.doUnmock('./HoverPreviewPopup');
  vi.doUnmock('./ui');
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

/** Fire the row's onMouseEnter via the mouseover event React synthesizes it from. */
function hoverRow(docId: string) {
  const row = container.querySelector(`[data-doc-id="${docId}"]`);
  if (!row) throw new Error(`row ${docId} not rendered`);
  act(() => {
    row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: null }));
  });
}

describe('DocumentTree hover-preview — feeds hovered docId on both surfaces', () => {
  it('on the authed surface, hovering a doc feeds its id to useDocumentPreview', () => {
    __isPublicShare = false;
    act(() => root.render(createElement(Harness)));

    hoverRow('doc-1');

    expect(previewMock).toHaveBeenCalledWith('doc-1');
  });

  it('regression: on /s/:token, hovering a doc STILL feeds its id to useDocumentPreview', () => {
    // WHY no longer gated: fetchDocumentContent is now public-aware — when a
    // public token is set via setPublicFileContext, it routes through the
    // anonymous /public/{token}/documents/{id} endpoint (no 401, no redirect).
    // See useDocumentPreview.test.ts for the routing assertion.
    __isPublicShare = true;
    act(() => root.render(createElement(Harness)));

    hoverRow('doc-1');

    expect(previewMock).toHaveBeenCalledWith('doc-1');
  });
});

describe('DocumentTree key indicator — public-share paints the icon orange', () => {
  it('a doc with public_share gets the .key-public class on its icon', () => {
    appState.documentTree = [makeNode('doc-pub', 'Public', { public_share: true })];
    act(() => root.render(createElement(Harness)));

    const icon = container.querySelector('.doc-icon.key-public');
    expect(icon, 'expected a .doc-icon.key-public span').not.toBeNull();
  });

  it('a plain doc (no key, no public share) has no key-* class', () => {
    appState.documentTree = [makeNode('doc-plain', 'Plain')];
    act(() => root.render(createElement(Harness)));

    expect(container.querySelector('.doc-icon.key-public')).toBeNull();
    expect(container.querySelector('.doc-icon.key-agent')).toBeNull();
    expect(container.querySelector('.doc-icon.key-widget')).toBeNull();
  });

  it('agent capability wins over public_share (red, not orange)', () => {
    appState.documentTree = [makeNode('doc-agent', 'Agent', {
      key_capabilities: ['agent'], public_share: true,
    })];
    act(() => root.render(createElement(Harness)));

    expect(container.querySelector('.doc-icon.key-agent')).not.toBeNull();
    expect(container.querySelector('.doc-icon.key-public')).toBeNull();
  });
});

describe('DocumentTree reference plaques — source and parent never coincide', () => {
  it('a reference opened from its own parent paints the row ref-parent only', () => {
    // Regression: both `active` (ref source) and `ref-parent` on one row let the
    // later `.active .doc-item-actions` rule paint a gray plate under a blue row.
    appState.currentReference = { document_id: 'doc-1' };
    appState.referenceSourceDocId = 'doc-1';
    act(() => root.render(createElement(Harness)));

    const row = container.querySelector('[data-doc-id="doc-1"]')!;
    expect(row.classList.contains('ref-parent')).toBe(true);
    expect(row.classList.contains('active')).toBe(false);
  });

  it('a reference opened from another doc paints that doc active, the parent ref-parent', () => {
    appState.documentTree = [makeNode('doc-1', 'Doc 1'), makeNode('doc-2', 'Doc 2')];
    appState.currentReference = { document_id: 'doc-2' };
    appState.referenceSourceDocId = 'doc-1';
    act(() => root.render(createElement(Harness)));

    const src = container.querySelector('[data-doc-id="doc-1"]')!;
    const parent = container.querySelector('[data-doc-id="doc-2"]')!;
    expect(src.classList.contains('active')).toBe(true);
    expect(src.classList.contains('ref-parent')).toBe(false);
    expect(parent.classList.contains('ref-parent')).toBe(true);
    expect(parent.classList.contains('active')).toBe(false);
  });

  // Panel quick preview: the reference takes part in nothing but its own tab —
  // the current document stays `active`, no row is `ref-parent`.
  it('panel mode + open cross-doc ref → current doc active, no ref-parent anywhere', () => {
    appState.documentTree = [makeNode('doc-1', 'Doc 1'), makeNode('doc-2', 'Doc 2')];
    appState.currentReference = { document_id: 'doc-2' };
    appState.referenceSourceDocId = null;
    // The row reads the mode of the CURRENT document (app-store), not useParams.
    appState.currentDocument = { document_id: 'doc-1' };
    __docUi = { 'doc-1': { refOpenMode: 'panel' } };
    act(() => root.render(createElement(Harness)));

    const cur = container.querySelector('[data-doc-id="doc-1"]')!;
    const parent = container.querySelector('[data-doc-id="doc-2"]')!;
    expect(cur.classList.contains('active')).toBe(true);
    expect(cur.classList.contains('ref-parent')).toBe(false);
    expect(parent.classList.contains('ref-parent')).toBe(false);
    expect(parent.classList.contains('active')).toBe(false);
  });
});
