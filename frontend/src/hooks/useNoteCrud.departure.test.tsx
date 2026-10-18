/** Leaving a new, untouched note's thread by any route cancels it (useNoteCrud departure effect). */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement, act, StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../api/client', () => ({
  apiClient: {
    post: vi.fn(),
    get: vi.fn().mockResolvedValue([]),
    patch: vi.fn(),
    delete: vi.fn().mockResolvedValue({}),
  },
}));

import { apiClient } from '../api/client';
import { useNoteCrud } from './useNoteCrud';
import { useAppStore } from '../store/app-store';
import { useNoteStore } from '../store/note-store';
import { useNoteChatStore } from '../store/note-chat-store';
import type { Document, Project } from '../types';

function Host() {
  useNoteCrud({ highlightNote: () => {} });
  return null;
}

let root: Root;
const flush = () => act(async () => { await Promise.resolve(); });

async function openFreshNote(id: string) {
  (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
    session_id: id, project_id: 'p1', document_id: 'doc-1', reference_id: null, is_note: true,
    title: '', created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z',
  });
  await act(async () => {
    await useNoteChatStore.getState().createNoteSession({ projectId: 'p1', documentId: 'doc-1' });
    useNoteStore.getState().setActiveNoteThreadId(id, 'doc-1');
  });
}

beforeEach(() => {
  (apiClient.delete as ReturnType<typeof vi.fn>).mockClear();
  useNoteChatStore.getState().reset();
  useNoteStore.setState({ activeNoteThreadId: null, documentNoteThreads: {} });
  useAppStore.setState({
    currentProject: { project_id: 'p1' } as Project,
    currentDocument: { document_id: 'doc-1', content: '' } as Document,
  });
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

describe('useNoteCrud — departure from a new note', () => {
  it('panel unmount (tab switch / panel close) deletes the untouched new note and drops the open-thread tint', async () => {
    act(() => root.render(createElement(Host)));
    await openFreshNote('n1');
    act(() => root.unmount());
    await flush();
    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/n1');
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
    expect(useNoteStore.getState().documentNoteThreads['doc-1']).toBeNull();
  });

  it('switching to another thread (document switch) deletes the abandoned one and forgets it for its document', async () => {
    act(() => root.render(createElement(Host)));
    await openFreshNote('n1');
    act(() => useNoteStore.setState({ activeNoteThreadId: null }));
    await flush();
    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/n1');
    expect(useNoteStore.getState().documentNoteThreads['doc-1']).toBeNull();
    act(() => root.unmount());
  });

  it('a note with a draft survives the tab switch and stays open', async () => {
    act(() => root.render(createElement(Host)));
    await openFreshNote('n1');
    useNoteChatStore.getState().setDraftForSession('n1', 'half a thought');
    act(() => root.unmount());
    await flush();
    expect(apiClient.delete).not.toHaveBeenCalled();
    expect(useNoteStore.getState().activeNoteThreadId).toBe('n1');
  });

  it('StrictMode double-invoke on mount does not delete the note being shown', async () => {
    await openFreshNote('n1');
    act(() => root.render(createElement(StrictMode, null, createElement(Host))));
    await flush();
    expect(apiClient.delete).not.toHaveBeenCalled();
    expect(useNoteStore.getState().activeNoteThreadId).toBe('n1');
    act(() => root.unmount());
  });
});
