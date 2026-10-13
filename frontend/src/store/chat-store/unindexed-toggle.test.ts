/** setChatUnindexed — the per-chat "not in the search index" toggle: optimistic
 * flip of every listed row of the conversation, rollback + error toast on a
 * failed PATCH, and a ghost holds the choice client-side until first send. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

const { patchMock, toastMock, loadSessionsMock } = vi.hoisted(() => ({
  patchMock: vi.fn(), toastMock: vi.fn(), loadSessionsMock: vi.fn(),
}));
vi.mock('../../api/client', () => ({
  apiClient: { patch: (...args: unknown[]) => patchMock(...args) },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), {
    getState: () => ({
      showToast: toastMock, accessLevel: 'full', currentProject: { project_id: 'p1' },
    }),
  }),
}));

import { createUnindexedActions } from './sessions-slice/unindexed-actions';
import type { ChatState } from './types';

function sess(id: string, threadId: string | null, unindexed = false): ChatState['sessions'][number] {
  return { session_id: id, thread_id: threadId, unindexed, title: id } as unknown as ChatState['sessions'][number];
}

function buildStore(activeSessionId: string | null) {
  return create<ChatState>((set, get) => ({
    ...createUnindexedActions(set, get),
    sessions: [sess('root', null), sess('branch', 'root'), sess('other', null)],
    activeSessionId,
    documentId: 'd1',
    ghostUnindexed: false,
    loadSessions: loadSessionsMock,
  } as unknown as ChatState));
}

const flags = (store: ReturnType<typeof buildStore>) =>
  Object.fromEntries(store.getState().sessions.map(s => [s.session_id, !!s.unindexed]));

describe('setChatUnindexed', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('flips every listed row of the thread and PATCHes the active session', async () => {
    patchMock.mockResolvedValue({ session_id: 'branch', unindexed: true });
    const store = buildStore('branch');
    await store.getState().setChatUnindexed(true);
    expect(patchMock).toHaveBeenCalledWith('/chat/sessions/branch', { unindexed: true });
    expect(flags(store)).toEqual({ root: true, branch: true, other: false });
    expect(toastMock).not.toHaveBeenCalled();
  });

  it('re-reads the list after a stored toggle — the server also flips continuations it cannot match by thread', async () => {
    patchMock.mockResolvedValue({ session_id: 'branch', unindexed: true });
    const store = buildStore('branch');
    await store.getState().setChatUnindexed(true);
    expect(loadSessionsMock).toHaveBeenCalledWith('p1', 'd1');
  });

  it('rolls back every flipped row and shows an error when the PATCH fails', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    const store = buildStore('root');
    const pending = store.getState().setChatUnindexed(true);
    // Optimistic: painted before the server answers.
    expect(flags(store)).toEqual({ root: true, branch: true, other: false });
    await pending;
    expect(flags(store)).toEqual({ root: false, branch: false, other: false });
    expect(toastMock).toHaveBeenCalledWith(expect.any(String), 'error');
    expect(loadSessionsMock).not.toHaveBeenCalled();
  });

  it('a ghost holds the choice client-side — no PATCH', async () => {
    const store = buildStore(null);
    await store.getState().setChatUnindexed(true);
    expect(patchMock).not.toHaveBeenCalled();
    expect(store.getState().ghostUnindexed).toBe(true);
  });
});
