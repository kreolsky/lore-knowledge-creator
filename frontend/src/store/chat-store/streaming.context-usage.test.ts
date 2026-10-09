import { describe, it, expect, vi, beforeEach } from 'vitest';

// context_usage frame handler updates the active session row's context_tokens_used
// in sessions[] (one update per turn, not per token). Driven over the harness
// transport: the POST resolves, the frames arrive as WS envelopes through
// dispatchChatFrame.

const showToast = vi.fn();
vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ currentUser: { user_id: 'u1', name: 'U' }, showToast }) },
}));
const postMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: { post: (...a: unknown[]) => postMock(...a) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));

import { streamCompletion, dispatchChatFrame } from './streaming';
import type { ChatState, Set } from './types';

function makeStore(sessions = [{ session_id: 's1', model: 'deepseek/flash' }] as unknown as ChatState['sessions']) {
  let state = {
    sessions,
    messages: [],
    streaming: null,
    conversation: [],
    turnRanges: {},
    turnStartSeq: null,
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  return { get, set };
}

const IDS = { type: 'ids', user_message_id: 'um', assistant_message_id: 'am' };
const CU = { type: 'context_usage', used: 12345, cap: 262144 };
const TURN_CLOSED = { type: 'turn_closed' };

/** Drive one harness turn: register + POST, feed the frames, close the turn. */
async function drive(
  frames: Record<string, unknown>[],
  sessions = [{ session_id: 's1', model: 'deepseek/flash' }] as unknown as ChatState['sessions'],
) {
  const { get, set } = makeStore(sessions);
  const p = streamCompletion(get, set, {
    sessionId: 's1', body: {}, signal: new AbortController().signal,
    userParentId: null, userContent: 'hi',
  });
  dispatchChatFrame(get, set, 's1', IDS);
  for (const f of frames) dispatchChatFrame(get, set, 's1', f);
  dispatchChatFrame(get, set, 's1', TURN_CLOSED);
  await p;
  return get;
}

describe('context_usage frame handler', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  it('writes used AND cap onto the active session row', async () => {
    const get = await drive([CU]);
    const s = get().sessions.find(x => x.session_id === 's1');
    expect(s?.context_tokens_used).toBe(12345);
    // The frame's cap becomes the row's LIVE context_window (the gauge's
    // denominator) — a stale /models fallback self-corrects on every turn.
    expect(s?.context_window).toBe(262144);
  });

  it('overwrites the prior value on a second turn (new value, not accumulate)', async () => {
    const get = await drive([CU, { ...CU, used: 20000, cap: 1000000 }]);
    // Two context_usage frames in one turn: the LAST one wins (per-turn snapshot).
    const s = get().sessions.find(x => x.session_id === 's1');
    expect(s?.context_tokens_used).toBe(20000);
    expect(s?.context_window).toBe(1000000);
  });

  it('does not touch other sessions', async () => {
    const other = { session_id: 's2', model: 'm' } as never;
    const get = await drive([CU], [
      { session_id: 's1', model: 'deepseek/flash' },
      other,
    ] as unknown as ChatState['sessions']);
    const untouched = get().sessions.find(x => x.session_id === 's2');
    expect(untouched?.context_tokens_used).toBeUndefined();
    expect(untouched?.context_window).toBeUndefined();
  });
});
