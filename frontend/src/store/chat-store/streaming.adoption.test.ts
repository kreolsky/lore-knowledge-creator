/** reload-mid-turn ADOPTION.
 *
 * A reload mid-turn renders the open turn as STREAMING (the
 * store's streaming slot non-null, the assembler boundary at the open
 * window's min), never a settled partial row that flips on the first live
 * frame. The messages GET marks the open row (`open_turn`, backend half in
 * routes.chat.messages._mark_open_turn); adoptOpenTurn re-seats the slot +
 * registers the sink so live frames CONTINUE into the same window, and the
 * browser-WS-gap twin: a reload that shows the turn ENDED closes a stale
 * adopted slot instead of hanging on a terminal that died with the socket.
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

import { streamCompletion, dispatchChatFrame, adoptOpenTurn, hasOpenHarnessTurn } from './streaming';
import { replaceWindowFromRows, stripFrames, __flushFeedPublishForTest } from './conversation-feed';
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

const chunk = (seq: number) =>
  // A real assembler payload (replaceWindow rebuilds the location index, which
  // reads data.turn/data.step — a bare {turn, step} dies, unlike the live
  // append path's tolerance). Real v4 log shape: the settled `assistant/message`.
  ({
    type: 'dsh_event', kind: 'assistant/message', seq, surfaceOp: 'append',
    data: { turn: 1, step: 1, message: { id: `m${seq}`, role: 'assistant', content: [{ type: 'text', text: 'o' }], source: { kind: 'model', provider: 'lore', model: 'test' } }, stream: [] },
  });
const DONE = { type: 'done', content: 'Hello world' };
const IDS = { type: 'ids', user_message_id: 'um', assistant_message_id: 'am' };

/** A reload's raw rows for a mid-turn session: one open assistant row. */
function openRows(frames: unknown[]) {
  return [{
    message_id: 'am', chat_id: 's1', parent_id: 'um', role: 'assistant' as const,
    content: 'partial', created_at: '2026-01-01', open_turn: true, frames,
  }];
}

describe('adoptOpenTurn — the reload seats the open turn', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  it('leave mid-turn and return: the slot re-seats for the chat and the turn settles', async () => {
    const { get, set } = makeStore('s-back');
    // The turn starts while the chat is shown.
    const p = streamCompletion(get, set, {
      sessionId: 's-back', body: {}, signal: new AbortController().signal,
    });
    dispatchChatFrame(get, set, 's-back', IDS);

    // Leave (another chat shown): the slot drops, the registration stands.
    set({ activeSessionId: 'other', streaming: null } as Partial<ChatState>);
    expect(hasOpenHarnessTurn('s-back')).toBe(true);

    // Return: setActiveSession + the reload's rows (one open row) re-seat.
    set({ activeSessionId: 's-back' } as Partial<ChatState>);
    const rows = openRows([chunk(4)]);
    replaceWindowFromRows(rows, 's-back', set);
    adoptOpenTurn(get, set, 's-back', rows);
    set({ messages: rows.map(stripFrames) as ChatState['messages'] });

    expect(get().streaming?.sessionId).toBe('s-back');
    expect(get().streaming?.messageId).toBe('am');

    // A following frame grows the conversation — replay + live, no node lost.
    dispatchChatFrame(get, set, 's-back', chunk(7));
    __flushFeedPublishForTest(set);
    expect(get().conversation).toHaveLength(2);

    dispatchChatFrame(get, set, 's-back', DONE);
    dispatchChatFrame(get, set, 's-back', { type: 'turn_closed' });
    await p;
    expect(get().streaming).toBeNull();
    expect(get().turnRanges['am']).toEqual({ min: 4, max: 7 });
  });
});
