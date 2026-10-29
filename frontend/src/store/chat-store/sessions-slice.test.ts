/** sessions-slice — updateSession (last_message_at preservation) + the ghost/
 * create slot rule: leaving a streaming chat drops ONLY the shown slot, never
 * the other chat's turn registration. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

// Mock only what the touched paths hit: apiClient.patch/post + the app-store
// toast/access guard. The pending-patch helpers (./inflight) run real.
const patchMock = vi.fn();
const postMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: {
    patch: (...args: unknown[]) => patchMock(...args),
    post: (...args: unknown[]) => postMock(...args),
  },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), {
    getState: () => ({ showToast: vi.fn(), accessLevel: 'full', currentUser: { user_id: 'u1', name: 'U' } }),
  }),
}));

import { createSessionsSlice } from './sessions-slice';
import { streamCompletion, hasOpenHarnessTurn, dispatchChatFrame } from './streaming';
import { clearChatCaches } from './reset-registry';
import type { ChatState } from './types';

function sess(id: string, lm: string | null, updated: string): ChatState['sessions'][number] {
  return {
    session_id: id,
    document_id: null,
    reference_id: null,
    user_id: 'u1',
    title: `t-${id}`,
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    updated_at: updated,
    last_message_at: lm,
  } as unknown as ChatState['sessions'][number];
}

function buildStore(sessions: ChatState['sessions']) {
  return create<ChatState>((set, get) => ({
    ...createSessionsSlice(set, get),
    sessions,
    activeSessionId: 's1',
    messages: [],
    streaming: null,
  } as unknown as ChatState));
}

describe('the shown slot belongs to the active chat — leaving drops only the slot', () => {
  // The slot invariant (see types.ts): streaming === null || its sessionId is
  // the active chat. Leaving a streaming chat (ghost, create) must clear the
  // SLOT while the other chat's turn registration stands — its frames keep
  // arriving.
  beforeEach(() => {
    vi.clearAllMocks();
    clearChatCaches();
    postMock.mockResolvedValue({ accepted: true });
  });

  function storeWithOtherChatTurn(): ReturnType<typeof buildStore> {
    const store = buildStore([sess('s1', null, '2026-01-01'), sess('other', null, '2026-01-02')]);
    // A live turn of ANOTHER chat: registration + slot (the slot's owner is
    // that chat while it is shown).
    seatSlotFor(store, 'other');
    void streamCompletion(store.getState, store.setState, {
      sessionId: 'other', body: {}, signal: new AbortController().signal,
    });
    return store;
  }

  function seatSlotFor(
    store: ReturnType<typeof buildStore>, sessionId: string,
  ): void {
    store.setState({
      activeSessionId: sessionId,
      streaming: { sessionId, messageId: null, content: '', controller: null },
    } as unknown as Partial<ChatState>);
  }

  it('startGhostChat clears the slot; the other chat\'s registration stands', () => {
    const store = storeWithOtherChatTurn();
    expect(hasOpenHarnessTurn('other')).toBe(true);

    store.getState().startGhostChat();

    expect(store.getState().activeSessionId).toBeNull();
    expect(store.getState().streaming).toBeNull();
    expect(hasOpenHarnessTurn('other')).toBe(true);
  });

  it('createSession clears the slot for the fresh chat; the other chat\'s registration stands', async () => {
    const store = storeWithOtherChatTurn();
    postMock.mockResolvedValue({
      session_id: 'fresh', document_id: null, reference_id: null, user_id: 'u1',
      title: '', model: 'm', system_prompt_id: null, context_ids: [],
      created_at: '2026-01-01', updated_at: '2026-01-01',
    });

    await store.getState().createSession({ projectId: 'p1' });

    expect(store.getState().activeSessionId).toBe('fresh');
    expect(store.getState().streaming).toBeNull();
    expect(hasOpenHarnessTurn('other')).toBe(true);
  });
});

describe('setActiveSession — returning to a chat whose turn still runs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearChatCaches();
    postMock.mockResolvedValue({ accepted: true });
  });

  it('seats the slot from the registration at once (Stop before the reload lands), keyed to that chat', () => {
    const store = buildStore([sess('s1', null, '2026-01-01'), sess('other', null, '2026-01-02')]);
    store.setState({ activeSessionId: 'other' } as Partial<ChatState>);
    void streamCompletion(store.getState, store.setState, {
      sessionId: 'other', body: {}, signal: new AbortController().signal,
    });
    dispatchChatFrame(store.getState, store.setState, 'other',
      { type: 'ids', user_message_id: 'um', assistant_message_id: 'am' });
    store.getState().startGhostChat();
    expect(store.getState().streaming).toBeNull();

    // loadMessages is the reload (stubbed: the GET is not under test here).
    store.setState({ loadMessages: vi.fn(async () => {}) } as unknown as Partial<ChatState>);
    store.getState().setActiveSession('other');

    const slot = store.getState().streaming;
    expect(slot?.sessionId).toBe('other');
    expect(slot?.messageId).toBe('am');
    expect(slot?.controller).toBeTruthy();
  });

  it('a chat with no open turn opens with no slot', () => {
    const store = buildStore([sess('s1', null, '2026-01-01'), sess('idle', null, '2026-01-02')]);
    store.setState({ loadMessages: vi.fn(async () => {}) } as unknown as Partial<ChatState>);
    store.getState().setActiveSession('idle');
    expect(store.getState().streaming).toBeNull();
  });
});
