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
import { ACTIVE_TOGGLE_CLS } from './references/ref-utils';

const NOW = '2026-01-01T00:00:00Z';

const PROJECT: Project = {
  project_id: 'p1', name: 'P1', status: 'active', project_context: '', index_doc_id: 'root',
  voice_recording_doc_id: null, last_accessed_doc_id: null, owner_id: null, is_public: false,
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
const FILE_REF: Reference = { ...REF, reference_id: 'ref-3', title: 'Archive', media_type: 'file', file_path: '/a.zip' };

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
      createElement('div', { 'data-testid': 'refcard', 'data-ref': props.reference.reference_id }),
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

  it('panel + open AUDIO reference: media bar above the editor', async () => {
    await mount('panel', AUDIO_REF);
    expect(q('[data-testid="panel-media-bar"]').length).toBe(1);
    expect(q('[data-testid="panel-editor"]').length).toBe(1);
  });

  it('panel + open FILE reference: the archive card (media bar) renders above the editor', async () => {
    await mount('panel', FILE_REF);
    expect(q('[data-testid="panel-media-bar"]').length).toBe(1);
    expect(q('[data-testid="panel-editor"]').length).toBe(1);
  });

  it('← Refs on the plaque clears currentReference and the list comes back', async () => {
    await mount('panel', REF);
    const back = q('[data-testid="refs-panel-plaque"] button')[0];
    await act(async () => { back.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(0);
    expect(q('[data-testid="refcard"]').length).toBe(2);
  });

  it('panel + no open reference: the list renders, no plaque', async () => {
    await mount('panel', null);
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(0);
    expect(q('[data-testid="refcard"]').length).toBe(2);
  });

  it("'center' + open reference: the list renders, no plaque, no panel editor", async () => {
    await mount('center', REF);
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(0);
    expect(q('[data-testid="panel-editor"]').length).toBe(0);
    expect(q('[data-testid="refcard"]').length).toBe(2);
  });

  it('the chosen mode is highlighted with NO reference open — the user sees it before clicking', async () => {
    await mount('split', null);
    const byTitle = (t: string) => container.querySelector(`button[title="${t}"]`) as HTMLButtonElement;
    expect(byTitle('toggleSplitView').className).toContain(ACTIVE_TOGGLE_CLS);
    expect(byTitle('toggleRefInPanel').className).not.toContain(ACTIVE_TOGGLE_CLS);
    await act(async () => { byTitle('toggleRefInPanel').click(); });
    expect(byTitle('toggleRefInPanel').className).toContain(ACTIVE_TOGGLE_CLS);
    expect(byTitle('toggleSplitView').className).not.toContain(ACTIVE_TOGGLE_CLS);
  });

  it('the two toggles set their own mode, and the active one returns to center', async () => {
    await mount('center', REF);
    const byTitle = (t: string) => container.querySelector(`button[title="${t}"]`) as HTMLButtonElement;
    await act(async () => { byTitle('toggleRefInPanel').click(); });
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('panel');
    // Panel mode swaps the toolbar for the plaque; go back to the list to reach the toggles.
    await act(async () => { useAppStore.setState({ currentReference: null }); });
    await act(async () => { byTitle('toggleSplitView').click(); });
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('split');
    await act(async () => { byTitle('toggleSplitView').click(); });
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('center');
  });

  it('archive from the plaque: the reference STAYS open in the panel, now archived', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValue({ ...REF, archived: true });
    await mount('panel', REF);
    useAppStore.setState({ references: [REF, AUDIO_REF] });
    const archiveBtn = container.querySelector('button[title="archiveReference"]')!;
    await act(async () => { archiveBtn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(apiClient.patch).toHaveBeenCalledWith('/references/ref-1', { archived: true });
    expect(useAppStore.getState().currentReference?.archived).toBe(true);
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(1);
    expect(q('[data-testid="refcard"]').length).toBe(0);
    expect(container.querySelector('button[title="deleteReferencePermanently"]')).not.toBeNull();
    expect(container.querySelector('button[title="restoreReference"]')).not.toBeNull();
  });

  it('armed delete of an archived reference from the plaque: back to the list, reference gone', async () => {
    const archived = { ...REF, archived: true };
    // The delete hook's unmount flush is fire-and-forget (`.catch`) — it needs a promise back.
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue({});
    await mount('panel', archived);
    useAppStore.setState({ references: [archived, AUDIO_REF] });
    const trash = () => container.querySelector('button[title="deleteReferencePermanently"]')!;
    await act(async () => { trash().dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(useAppStore.getState().currentReference).not.toBeNull();
    await act(async () => { trash().dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(0);
    expect(useAppStore.getState().references.map(r => r.reference_id)).toEqual(['ref-2']);
  });

  it('the pressed eye on the plaque switches the doc back to center; the reference stays open (moves to the center)', async () => {
    await mount('panel', REF);
    const eye = container.querySelector('[data-testid="refs-panel-plaque"] button[title="toggleRefInPanel"]')!;
    await act(async () => { eye.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('center');
    expect(useAppStore.getState().currentReference).toBe(REF);
    expect(q('[data-testid="refs-panel-plaque"]').length).toBe(0);
    expect(q('[data-testid="refcard"]').length).toBe(2);
  });

  it('goto-parent on the plaque: parent doc switched to panel mode, reference navigation emitted (keeps the ref open there)', async () => {
    const { on, off } = await import('../events');
    const seen: unknown[] = [];
    const handler = (p: { referenceId: string }) => { seen.push(p); };
    on('navigate-to-reference', handler);
    useAppStore.setState({ documents: [mkDoc('root', null), mkDoc('doc-1', 'root'), mkDoc('doc-2', 'root')], currentDocument: mkDoc('doc-2', 'root') });
    await mount('panel', REF, 'doc-2');
    const goto = container.querySelector('[data-testid="refs-panel-plaque"] [data-title-slot] button')!;
    await act(async () => { goto.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    off('navigate-to-reference', handler);
    expect(useUIStore.getState().getRefOpenMode('doc-1')).toBe('panel');
    expect(useUIStore.getState().documents['doc-1'].rightPanelTab).toBe('refs');
    expect(useUIStore.getState().documents['doc-1'].rightPanelOpen).toBe(true);
    expect(seen).toEqual([{ referenceId: 'ref-1', sourceDocId: null }]);
  });
});
