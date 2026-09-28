/**
 * navigate-to-reference, cross-doc branch: the TARGET document is committed to the
 * store BEFORE the URL changes (the same pre-commit the Header's navigate-to-document
 * path does), with the reference as the winner. Without it, DocumentPage's URL-driven
 * prefetch commit is dropped as stale (INVARIANT(doc-desync)) and the previous
 * document stays on screen under the new URL.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MemoryRouter } from 'react-router-dom';
import type { Document, Reference } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const navigateSpy = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-router-dom')>();
  return { ...mod, useNavigate: () => navigateSpy };
});

import { useEditorEvents } from './useEditorEvents';
import { useAppStore } from '../store/app-store';
import { emit } from '../events';

const NOW = '2026-01-01T00:00:00Z';
const mkDoc = (id: string): Document => ({
  document_id: id, project_id: 'p1', parent_id: null, title: id, content: '', path: '', is_index: false, created_at: NOW, updated_at: NOW,
});
const REF: Reference = {
  reference_id: 'ref-1', project_id: 'p1', document_id: 'doc-parent', title: 'R', media_type: 'markdown',
  source_url: null, content: 'body', processing_status: 'ready', file_path: null, file_meta: null, created_at: NOW, updated_at: NOW,
};

function Host() {
  const currentDocument = useAppStore(s => s.currentDocument);
  useEditorEvents({ editorViewRef: { current: null }, currentDocument });
  return null;
}

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  navigateSpy.mockReset();
  useAppStore.setState({
    currentProject: { project_id: 'p1', name: 'P', status: 'active', project_context: '', index_doc_id: 'root', voice_recording_doc_id: null, last_accessed_doc_id: null, owner_id: null, is_public: false, my_access: 'full', created_at: NOW },
    documents: [mkDoc('doc-child'), mkDoc('doc-parent')],
    currentDocument: mkDoc('doc-child'),
    currentReference: null,
    references: [REF],
  });
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(MemoryRouter, null, createElement(Host))));
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

describe('navigate-to-reference — cross-doc', () => {
  it('commits the parent document with the reference open BEFORE navigating', async () => {
    await act(async () => { emit('navigate-to-reference', { referenceId: 'ref-1' }); });
    await act(async () => { await new Promise(r => setTimeout(r, 0)); });
    const s = useAppStore.getState();
    expect(s.currentDocument?.document_id).toBe('doc-parent');
    expect(s.currentReference?.reference_id).toBe('ref-1');
    expect(navigateSpy).toHaveBeenCalledWith('/docs/doc-parent');
    // Default: the doc we left is the reference's source (the tree keeps it highlighted).
    expect(s.referenceSourceDocId).toBe('doc-child');
  });

  it('sourceDocId: null — a goto-parent jump leaves NO source (the left doc is not highlighted)', async () => {
    await act(async () => { emit('navigate-to-reference', { referenceId: 'ref-1', sourceDocId: null }); });
    await act(async () => { await new Promise(r => setTimeout(r, 0)); });
    const s = useAppStore.getState();
    expect(s.currentDocument?.document_id).toBe('doc-parent');
    expect(s.referenceSourceDocId).toBeNull();
  });
});
