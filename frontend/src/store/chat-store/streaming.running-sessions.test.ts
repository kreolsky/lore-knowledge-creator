/** The per-session running set (`runningSessions`): a `dsh_status` frame of
 * ANY chat — not just the shown one — folds into the set, so the chat list
 * can mark the chats whose turn is still open while another chat is open.
 * `dshRunning` keeps its own, active-only meaning (the INVARIANT in
 * inbox-slice.ts). */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ currentUser: { user_id: 'u1', name: 'U' }, showToast: vi.fn() }) },
}));
const postMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: { post: (...a: unknown[]) => postMock(...a) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
vi.mock('../ui-store', () => ({
  useUIStore: { getState: () => ({ setLastActiveChatSession: vi.fn() }) },
}));

import { dispatchChatFrame } from './streaming';
import { createMiscSlice } from './misc-slice';
import { clearChatCaches } from './reset-registry';
import type { ChatState, Set } from './types';

function makeStore(active = 's1') {
  let state = {
    activeSessionId: active,
    sessions: [], messages: [], streaming: null,
    conversation: [], turnRanges: {}, turnStartSeq: null,
    inboxQueue: [], dshRunning: false, liveSend: null,
    runningSessions: new Set<string>(),
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  return { get, set };
}

describe('runningSessions — the per-chat working set', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearChatCaches();
    postMock.mockResolvedValue({ accepted: true });
  });

  it("a running dsh_status for a NON-active chat adds it; dshRunning stays untouched", () => {
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    expect(get().runningSessions.has('other')).toBe(true);
    expect(get().dshRunning).toBe(false);
  });

  it('any non-running status removes the chat (its turn ended)', () => {
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'idle' });
    expect(get().runningSessions.has('other')).toBe(false);
  });

  it('the ACTIVE chat joins the set AND keeps dshRunning behaviour', () => {
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 's1', { type: 'dsh_status', status: 'running' });
    expect(get().runningSessions.has('s1')).toBe(true);
    expect(get().dshRunning).toBe(true);
    dispatchChatFrame(get, set, 's1', { type: 'dsh_status', status: 'idle' });
    expect(get().runningSessions.has('s1')).toBe(false);
    expect(get().dshRunning).toBe(false);
  });

  it('a repeated running frame writes NO new Set (rows must not re-render)', () => {
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    const before = get().runningSessions;
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    expect(get().runningSessions).toBe(before);
  });

  it('a non-status frame for a non-active chat never touches the set', () => {
    const { get, set } = makeStore('s1');
    const before = get().runningSessions;
    dispatchChatFrame(get, set, 'other', { type: 'ids', user_message_id: 'u', assistant_message_id: 'a', chat_id: 'other' });
    expect(get().runningSessions).toBe(before);
  });

  it('reset() empties the set (logout hygiene)', () => {
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    createMiscSlice(set, get).reset();
    expect(get().runningSessions.size).toBe(0);
  });
});
