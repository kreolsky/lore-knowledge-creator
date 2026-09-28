/** Plan "chat-draft-persistence" (store layer): the AI-chat composer draft lives in
 * chat-store as ONE shared string — session-agnostic, matching the prior in-mount
 * useState semantics (text already survived session switches) — so it survives
 * ChatPanel unmount on right-panel tab switches. In-memory only; cleared on send and
 * on logout reset() so the prior user's draft never renders after a same-tab
 * re-login. */
// @vitest-environment jsdom

import { describe, it, expect, vi } from 'vitest';
import { create } from 'zustand';

vi.mock('../../api/client', () => ({
  apiClient: {
    post: vi.fn(),
    patch: vi.fn(),
  },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), {
    getState: () => ({ accessLevel: 'full', showToast: vi.fn() }),
  }),
}));
vi.mock('../ui-store', () => ({
  useUIStore: {
    getState: () => ({
      getLastActiveChatSession: () => null,
      setLastActiveChatSession: vi.fn(),
      setRightPanelTab: vi.fn(),
      getRefOpenMode: () => 'center',
    }),
  },
}));
vi.mock('../../events', () => ({ emit: vi.fn() }));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
vi.mock('../../chat/context', () => ({
  hydrateFromSessions: vi.fn(),
  clearContextForSession: vi.fn(),
  setContextForSession: vi.fn(),
  addItemToContext: vi.fn(() => Promise.resolve()),
  getDerivedGhostContext: () => ({ docIds: [], refIds: [] }),
  ghostBaseTargets: () => ({ docs: [], refs: [] }),
  resetGhostDeltas: vi.fn(),
}));

import { createMiscSlice } from './misc-slice';
import type { ChatState } from './types';

function buildStore() {
  return create<ChatState>((set, get) => ({
    sessions: [],
    activeSessionId: null,
    messages: [],
    streaming: null,
    pendingImages: [],
    imageGen: {},
    ghostAgentAuto: false,
    ghostSystemPromptId: null,
    ghostModel: '',
    ghostRegion: null,
    ...createMiscSlice(set, get),
  } as unknown as ChatState));
}

describe('chat draft (shared, session-agnostic)', () => {
  it('setDraft writes the shared draft', () => {
    const store = buildStore();
    store.getState().setDraft('hello');
    expect(store.getState().draft).toBe('hello');
  });

  it('reset() clears the draft — logout hygiene', () => {
    const store = buildStore();
    store.getState().setDraft('hello');
    store.getState().reset();
    expect(store.getState().draft).toBe('');
  });
});
