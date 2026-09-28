/**
 * TOCTOU: an in-flight loadSessions resolving AFTER a soft
 * logout must NOT write the prior user's sessions back into the cache/store.
 *
 * Mechanism: loadSessions captures the logout epoch at start; clearUserScopedCaches
 * (logout) bumps it. A resolve that lands after the bump hits the epoch guard in the
 * race check and returns before sessionsCache.seed / set({ sessions }).
 *
 * Focused on the load path: builds a minimal store from createSessionsSlice and mocks
 * only what the pre-guard code touches (apiClient.get controllable + ui-store). The
 * guard returns before the resolver/hydrate run, so those need no mocks here.
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

const getMock = vi.fn<(...args: unknown[]) => Promise<unknown>>();
const postMock = vi.fn<(...args: unknown[]) => Promise<unknown>>();
vi.mock('../../api/client', () => ({
  apiClient: {
    get: (...args: unknown[]) => getMock(...args),
    post: (...args: unknown[]) => postMock(...args),
  },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), { getState: () => ({ showToast: vi.fn(), accessLevel: 'full' }) }),
}));
vi.mock('../ui-store', () => ({
  useUIStore: {
    getState: () => ({
      getLastActiveChatSession: () => null,
      setLastActiveChatSession: vi.fn(),
      getRefOpenMode: () => 'center',
    }),
  },
}));

import { createSessionsSlice, clearSessionsCache } from './sessions-slice';
import { clearUserScopedCaches } from '../logout-handlers';
import type { ChatState } from './types';

function buildStore() {
  return create<ChatState>((set, get) => ({
    ...createSessionsSlice(set, get),
    sessions: [],
    activeSessionId: null,
    documentId: null,
  } as unknown as ChatState));
}

function flush() {
  // Drain pending microtasks so a resumed async loadSessions settles.
  return new Promise(r => setTimeout(r, 0));
}

describe('loadSessions — TOCTOU (logout mid-flight)', () => {
  beforeEach(() => {
    clearSessionsCache();
    getMock.mockReset();
    postMock.mockReset();
  });

  it('drops a sessions write that resolves after a soft logout', async () => {
    const store = buildStore();
    let resolveGet!: (v: unknown) => void;
    getMock.mockImplementation(() => new Promise(r => { resolveGet = r; }));

    // In-flight load for the prior user (A).
    void store.getState().loadSessions('pA', 'dA');
    await flush();

    // Soft logout: bumps the epoch (and in the real app resets the store + cache).
    clearUserScopedCaches();
    expect(store.getState().sessions).toEqual([]);

    // A's fetch resolves AFTER the logout — must be dropped, not written back.
    resolveGet([{ session_id: 'A-secret' }]);
    await flush();

    expect(store.getState().sessions).toEqual([]);
  });

  it('still writes a fetch that resolves with no intervening logout', async () => {
    // Regression guard: the epoch check must not drop a NORMAL load (no logout mid-flight).
    const store = buildStore();
    getMock.mockResolvedValue([{ session_id: 'live' }]);

    void store.getState().loadSessions('pB', 'dB');
    await flush();

    expect(store.getState().sessions.map(s => s.session_id)).toContain('live');
  });

  it('drops a createSession insert that resolves after a soft logout', async () => {
    // Write-path TOCTOU: an in-flight session-create POST resolving after logout
    // must not insert the prior user's freshly-created session into the reset store.
    const store = buildStore();
    let resolvePost!: (v: unknown) => void;
    postMock.mockImplementation(() => new Promise(r => { resolvePost = r; }));

    // Full access gate passes; no pending PATCH → straight to the POST.
    const p = store.getState().createSession({ projectId: 'pA', documentId: 'dA', parentSessionId: null });
    await flush();

    clearUserScopedCaches(); // soft logout mid-POST (bumps the epoch)
    expect(store.getState().sessions).toEqual([]);

    resolvePost({ session_id: 'A-secret' });
    const result = await p;

    expect(result).toBeNull(); // no-op signal, consistent with the existing failure paths
    expect(store.getState().sessions).toEqual([]);   // prior user's session NOT inserted
    expect(store.getState().activeSessionId).toBeNull();
  });
});
