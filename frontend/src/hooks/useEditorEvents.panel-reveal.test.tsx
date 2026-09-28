/**
 * Panel (quick preview) mode: the Refs tab is revealed by the user's reference open
 * (navigate-to-reference), never by a restored currentReference — the tab the user
 * was on (e.g. Chat) survives F5 and returning to the document.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MemoryRouter } from 'react-router-dom';
import type { Document, Reference } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('react-router-dom', async (importOriginal) => {
  const mod = await importOriginal<typeof import('react-router-dom')>();
  return { ...mod, useNavigate: () => vi.fn() };
});

import { useEditorEvents } from './useEditorEvents';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { emit } from '../events';

const NOW = '2026-01-01T00:00:00Z';
const DOC: Document = {
  document_id: 'doc-1', project_id: 'p1', parent_id: null, title: 'D', content: '', path: '', is_index: false, created_at: NOW, updated_at: NOW,
};
const REF: Reference = {
  reference_id: 'ref-1', project_id: 'p1', document_id: 'doc-1', title: 'R', media_type: 'markdown',
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
  useAppStore.setState({ currentDocument: DOC, currentReference: null, references: [REF] });
  useUIStore.setState({ documents: {}, searchTabDocId: null });
  useUIStore.getState().setRightPanelTab('doc-1', 'chat');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(createElement(MemoryRouter, null, createElement(Host))));
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

const tab = () => useUIStore.getState().getDocState('doc-1').rightPanelTab;

describe('navigate-to-reference — panel-mode Refs reveal', () => {
  it('panel mode: opening a reference switches the doc to the Refs tab and opens the panel', async () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    await act(async () => { emit('navigate-to-reference', { referenceId: 'ref-1' }); });
    await act(async () => { await new Promise(r => setTimeout(r, 0)); });
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-1');
    expect(tab()).toBe('refs');
    expect(useUIStore.getState().getDocState('doc-1').rightPanelOpen).toBe(true);
  });

  it('center mode: opening a reference leaves the saved tab alone', async () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'center');
    await act(async () => { emit('navigate-to-reference', { referenceId: 'ref-1' }); });
    await act(async () => { await new Promise(r => setTimeout(r, 0)); });
    expect(tab()).toBe('chat');
  });

  it('panel mode: a RESTORED reference (F5 / returning to the doc) keeps the Chat tab', async () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    await act(async () => { useAppStore.setState({ currentReference: REF }); });
    expect(tab()).toBe('chat');
  });
});
