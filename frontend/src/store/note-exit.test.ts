/** exitNoteThread is the ONE exit
 * path — the "back to all notes" button (useNoteCrud.exitThread) and
 * NoteChatView's Escape branch both delegate here. The two rules this pins:
 * leaving an EMPTY thread deletes the session (no orphan empty cards), and
 * previousRightTab is restored only when it was explicitly set (an anchorless
 * note created from the panel has no tab to restore — restoring null would
 * close the right panel). */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn().mockResolvedValue({}),
  },
}));

import { apiClient } from '../api/client';
import { exitNoteThread } from './note-exit';
import { useNoteStore } from './note-store';
import { useNoteChatStore } from './note-chat-store';
import { useUIStore } from './ui-store';

function seedThread(overrides: Record<string, unknown> = {}) {
  useNoteChatStore.setState({
    sessions: [{
      session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
      is_note: true, title: '', mode: 'chat', model: '', message_count: 0,
      updated_at: '2026-10-22T10:00:00Z', created_at: '2026-10-22T10:00:00Z',
    } as never],
    activeSessionId: 'ns1',
    messages: [],
    messagesLoading: false,
    ...overrides,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({});
  useUIStore.setState({ documents: {}, searchTabDocId: null });
  useNoteStore.setState({
    activeNoteThreadId: null,
    documentNoteThreads: {},
    pendingNoteNavigation: null,
    connectedNoteId: null,
    previousRightTab: null,
  });
});

describe('exitNoteThread — the shared exit path', () => {
  it('deletes a provably empty thread and restores previousRightTab', () => {
    seedThread();
    useNoteStore.setState({
      activeNoteThreadId: 'ns1',
      documentNoteThreads: { d1: 'ns1' },
      previousRightTab: 'chat',
    });

    exitNoteThread('d1');

    // Empty thread deleted (optimistic removal + DELETE issued).
    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/ns1');
    expect(useNoteChatStore.getState().sessions).toEqual([]);
    // Thread state cleared, including the per-doc memory.
    const ns = useNoteStore.getState();
    expect(ns.activeNoteThreadId).toBeNull();
    expect(ns.documentNoteThreads).toEqual({ d1: null });
    // The session is no longer active; the editor-side tab is restored once.
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
    expect(ns.previousRightTab).toBeNull();
    expect(useUIStore.getState().getDocState('d1').rightPanelTab).toBe('chat');
  });

  it('keeps a NON-empty thread (only the exit state changes)', () => {
    seedThread({
      messages: [{ message_id: 'm1', session_id: 'ns1', role: 'user', content: 'hi', created_at: 'x' } as never],
      sessions: [{
        session_id: 'ns1', project_id: 'p1', document_id: 'd1', reference_id: null,
        is_note: true, title: '', mode: 'chat', model: '', message_count: 1,
        updated_at: '2026-10-22T10:00:00Z', created_at: '2026-10-22T10:00:00Z',
      } as never],
    });
    useNoteStore.setState({ activeNoteThreadId: 'ns1', previousRightTab: 'chat' });

    exitNoteThread('d1');

    expect(apiClient.delete).not.toHaveBeenCalled();
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
    expect(useUIStore.getState().getDocState('d1').rightPanelTab).toBe('chat');
  });

  it('never restores a null previousRightTab — the notes tab stays put', () => {
    seedThread();
    useNoteStore.setState({ activeNoteThreadId: 'ns1', previousRightTab: null });
    useUIStore.getState().setRightPanelTab('d1', 'notes');

    exitNoteThread('d1');

    // Panel tab untouched: restoring null would close the right panel and
    // leave a blank surface under the tab bar.
    expect(useUIStore.getState().getDocState('d1').rightPanelTab).toBe('notes');
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
  });
});
