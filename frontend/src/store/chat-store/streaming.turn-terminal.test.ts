/** The replayed transport terminal's browser half.
 *
 * The plugin's replay appends a seq-anchored `turn_closed` per closed turn of
 * a non-live session, so a resync gap spanning several closed turns delivers
 * SEVERAL terminals — and the ONE duplicate the unsequenced live push cannot
 * dedup re-delivers a terminal the browser already consumed. The backend's
 * dispatch guards keep a stale terminal from a NEWER turn's registration; what
 * reaches a browser that already ended the turn must be a NO-OP (no
 * registration → dropped), so streaming ends exactly ONCE and the queue flush
 * fires exactly once.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const showToast = vi.fn();
vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ currentUser: { user_id: 'u1', name: 'U' }, showToast }) },
}));
const postMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: { post: (...a: unknown[]) => postMock(...a) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
const flushMock = vi.fn();
vi.mock('./queue-slice', () => ({
  scheduleTurnEndFlush: (...a: unknown[]) => flushMock(...a),
}));

import { streamCompletion, dispatchChatFrame, hasOpenHarnessTurn } from './streaming';
import type { ChatState, Set } from './types';

function makeStore(sessionId = 's1') {
  let state = {
    activeSessionId: sessionId,
    sessions: [{
      session_id: sessionId, document_id: null, reference_id: null, user_id: 'u1',
      title: 't', model: 'm', system_prompt_id: null, context_ids: [],
      updated_at: '2026-01-01', created_at: '2026-01-01', last_message_at: null,
    }],
    messages: [],
    selectedSiblings: {},
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
const DONE = { type: 'done', content: 'Hello world' };
const TURN_CLOSED = { type: 'turn_closed' };

describe('duplicate turn_closed frames (the replayed terminal)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    postMock.mockResolvedValue({ accepted: true });
  });

  it('two terminals end streaming ONCE — the second finds no registration and no-ops', async () => {
    // A gap spanning two closed turns delivers a terminal per turn; the
    // browser's turn is already over by the second. The queue flush (which
    // POSTs the follow-up) must fire exactly once.
    const { get, set } = makeStore('s-term');
    const p = streamCompletion(get, set, {
      sessionId: 's-term', body: { messages: [] }, signal: new AbortController().signal,
      userParentId: null, userContent: 'hi', optimisticUserId: 'temp-1',
    });

    dispatchChatFrame(get, set, 's-term', IDS);
    dispatchChatFrame(get, set, 's-term', DONE);
    dispatchChatFrame(get, set, 's-term', TURN_CLOSED);
    await p;
    // The duplicate: the replayed terminal of a turn the browser already
    // closed (or an older turn's terminal from the same gap).
    dispatchChatFrame(get, set, 's-term', TURN_CLOSED);
    dispatchChatFrame(get, set, 's-term', TURN_CLOSED);

    expect(get().streaming).toBeNull();
    expect(hasOpenHarnessTurn('s-term')).toBe(false);
    expect(flushMock).toHaveBeenCalledTimes(1);
    expect(flushMock).toHaveBeenCalledWith(expect.anything(), 's-term', 'done');
  });

  it('a terminal before any registration is a no-op (another tab / reload owns the turn)', () => {
    const { get, set } = makeStore('s-none');
    dispatchChatFrame(get, set, 's-none', TURN_CLOSED);
    expect(get().streaming).toBeNull();
    expect(flushMock).not.toHaveBeenCalled();
  });
});
