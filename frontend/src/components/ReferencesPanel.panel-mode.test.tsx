/**
 * 'panel' reference open mode: with a reference open, the Refs tab shows a plaque
 * (← Refs · title) plus the reference itself instead of the list; the plaque's
 * back button clears currentReference. Every other mode keeps the list.
 *
 * Mounts the REAL panel against real stores (harness per
 * ReferencesPanel.action-failures.test.tsx); Editor / ReferenceMediaBar / RefCard
 * are mocked down to markers so the render decision is what gets asserted.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import type { Document, Project, Reference } from '../types';
import type { RefOpenMode } from '../store/ui-store/documents-slice';

const NOW = '2026-01-01T00:00:00Z';

const PROJECT: Project = {
  project_id: 'p1', name: 'P1', status: 'active', project_context: '', index_doc_id: 'root',
  voice_recording_doc_id: null, last_accessed_doc_id: null, owner_id: null, audiences: [],
  my_access: 'full', created_at: NOW,
};

function mkDoc(id: string, parent_id: string | null): Document {
  return {
    document_id: id, project_id: 'p1', parent_id, title: id, content: '', path: '',
    is_index: false, created_at: NOW, updated_at: NOW,
  };
}

const REF: Reference = {
  reference_id: 'ref-1', project_id: 'p1', document_id: 'doc-1', title: 'Ref One Title',
  media_type: 'markdown', source_url: null, content: 'body', processing_status: 'ready',
  file_path: null, file_meta: null, created_at: NOW, updated_at: NOW,
};
const AUDIO_REF: Reference = { ...REF, reference_id: 'ref-2', title: 'Audio', media_type: 'audio', file_path: '/a.mp3' };

let container: HTMLDivElement;
let root: Root;
let ReferencesPanel: typeof import('./ReferencesPanel').ReferencesPanel;
let useAppStore: typeof import('../store/app-store').useAppStore;
let useUIStore: typeof import('../store/ui-store').useUIStore;
const editorProps: Array<{ entity?: Reference; role?: string; hideBanner?: boolean }> = [];

beforeEach(async () => {
  vi.resetModules();
  editorProps.length = 0;
  const stableT = (k: string) => k;
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: stableT }) }));
  vi.doMock('../api/references-fetch', () => ({ loadReferences: vi.fn().mockResolvedValue([REF, AUDIO_REF]) }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockResolvedValue(undefined), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  }));
  vi.doMock('./Editor', () => ({
    Editor: (props: { entity?: Reference; role?: string; hideBanner?: boolean }) => {
      editorProps.push(props);
      return createElement('div', { 'data-testid': 'panel-editor' });
    },
  }));
  vi.doMock('./editor/ReferenceMediaBar', () => ({
    ReferenceMediaBar: () => createElement('div', { 'data-testid': 'panel-media-bar' }),
  }));
  vi.doMock('./ui/DownloadMenu', () => ({
    DownloadMenu: () => createElement('div', { 'data-testid': 'panel-download' }),
  }));
  vi.doMock('./references/RefCard', () => ({
    RefCard: (props: { reference: Reference }) =>
      createElement('div', {
        'data-testid': 'refcard', 'data-ref': props.reference.reference_id,
        // The drag-reorder row attributes the real RefCard carries.
        'data-ref-id': props.reference.reference_id, 'data-ref-group': props.reference.document_id,
      }),
  }));

  ({ ReferencesPanel } = await import('./ReferencesPanel'));
  ({ useAppStore } = await import('../store/app-store'));
  ({ useUIStore } = await import('../store/ui-store'));

  useAppStore.setState({
    currentProject: PROJECT,
    documents: [mkDoc('root', null), mkDoc('doc-1', 'root')],
    currentDocument: mkDoc('doc-1', 'root'),
    currentReference: null,
    references: [],
    accessLevel: 'full',
  });
  useUIStore.setState({ documents: {}, isPublicShare: false });

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../i18n');
  vi.doUnmock('../api/references-fetch');
  vi.doUnmock('../api/client');
  vi.doUnmock('./Editor');
  vi.doUnmock('./editor/ReferenceMediaBar');
  vi.doUnmock('./references/RefCard');
  vi.doUnmock('./ui/DownloadMenu');
});

async function mount(mode: RefOpenMode, ref: Reference | null, docId = 'doc-1') {
  useUIStore.setState({ documents: { [docId]: { mainEntity: { type: 'document', id: docId }, rightPanelOpen: true, rightPanelTab: 'refs', refOpenMode: mode } } });
  useAppStore.setState({ currentReference: ref });
  act(() => root.render(createElement(ReferencesPanel)));
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

const q = (sel: string) => container.querySelectorAll(sel);

describe("ReferencesPanel — 'panel' open mode", () => {
  it('panel + open reference: plaque with the title, no list, Editor mounted with the reference as secondary', async () => {
    await mount('panel', REF);
    const plaque = q('[data-testid="refs-panel-plaque"]');
    expect(plaque.length).toBe(1);
    expect(plaque[0].textContent).toContain('references');
    expect(plaque[0].textContent).toContain('Ref One Title');
    expect(q('[data-testid="refcard"]').length).toBe(0);
    expect(q('[data-testid="panel-editor"]').length).toBe(1);
    expect(editorProps[0].entity).toBe(REF);
    expect(editorProps[0].role).toBe('secondary');
    expect(editorProps[0].hideBanner).toBe(true);
    expect(q('[data-testid="panel-media-bar"]').length).toBe(0);
  });

  it('after ← Refs the remounted list still starts a drag-reorder', async () => {
    await mount('panel', REF);
    const back = q('[data-testid="refs-panel-plaque"] button')[0];
    await act(async () => { back.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    const card = q('[data-testid="refcard"]')[0];
    // jsdom has no hit-testing; the drop-target lookup finds nothing (no indicator).
    const doc = document as unknown as { elementFromPoint: (x: number, y: number) => Element | null };
    doc.elementFromPoint = () => null;
    act(() => {
      card.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0, clientX: 10, clientY: 10 }));
      document.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: 10, clientY: 40 }));
    });
    // The drag hook marks an active drag by disabling text selection on <body>.
    expect(document.body.style.userSelect).toBe('none');
    act(() => { document.dispatchEvent(new MouseEvent('pointerup', { bubbles: true })); });
  });

  it('goto-parent on the plaque: the parent document OPENS, in panel mode, with the reference still open in its Refs tab', async () => {
    // The real document-navigation owner stands in for useDocumentNavigation's bus
    // handler (it needs a Router), so the assertion is the outcome, not an emitted event.
    const { on, off } = await import('../events');
    const { openDocument } = await import('../navigation/open-document');
    const navigated: string[] = [];
    const handler = ({ documentId }: { documentId: string }) => openDocument(documentId, { navigate: (url) => { navigated.push(String(url)); } });
    on('navigate-to-document', handler);
    useAppStore.setState({ documents: [mkDoc('root', null), mkDoc('doc-1', 'root'), mkDoc('doc-2', 'root')], currentDocument: mkDoc('doc-2', 'root') });
    await mount('panel', REF, 'doc-2');
    const goto = container.querySelector('[data-testid="refs-panel-plaque"] [data-title-slot] button')!;
    await act(async () => { goto.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    off('navigate-to-document', handler);
    expect(navigated.length).toBe(1);
    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-1');
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-1');
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('panel');
    expect(useUIStore.getState().documents['doc-1'].rightPanelTab).toBe('refs');
    expect(useUIStore.getState().documents['doc-1'].rightPanelOpen).toBe(true);
  });
});
