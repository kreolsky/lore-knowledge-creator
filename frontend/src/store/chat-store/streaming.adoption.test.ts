/** Plan agent-line-harness-lifecycle step 8 — reload-mid-turn ADOPTION.
 *
 * Decision 16: a reload mid-turn renders the open turn as STREAMING (the
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

import { streamCompletion, dispatchChatFrame, adoptOpenTurn } from './streaming';
import { replaceWindowFromRows, stripFrames } from './conversation-feed';
import type { ChatState, Set } from './types';
function makeStore(sessionId = 's1') {
  let state = {
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

const chunk = (seq: number) =>
  // A real assembler payload (replaceWindow rebuilds the location index, which
  // reads data.turn/data.step — a bare {turn, step} dies, unlike the live
  // append path's tolerance). v3 vocabulary: the settled `assistant/message`.
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

  it('seats streaming at the open row and continues live frames through the same window', () => {
    const { get, set } = makeStore();
    const rows = openRows([chunk(4)]);
    replaceWindowFromRows(rows, 's1', set);
    adoptOpenTurn(get, set, 's1', rows);
    // The load's onLoaded commits the (stripped) rows — the message the open
    // turn writes already exists on a reload.
    set({ messages: rows.map(stripFrames) as ChatState['messages'] });

    expect(get().streaming?.messageId).toBe('am');
    // The boundary is the OPEN window's min, not the tail: the whole open
    // turn renders in the streaming slot (Decision 16).
    expect(get().turnStartSeq).toBe(4);

    dispatchChatFrame(get, set, 's1', chunk(7));
    dispatchChatFrame(get, set, 's1', DONE);
    expect(get().streaming).toBeNull();
    // The settled window spans replay + live frames.
    expect(get().turnRanges['am']).toEqual({ min: 4, max: 7 });
    expect(get().messages.find(m => m.message_id === 'am')?.content).toBe('Hello world');
  });

  it('rows with no open mark adopt nothing', () => {
    const { get, set } = makeStore();
    const rows = [{ message_id: 'am', role: 'assistant' as const, content: 'settled', created_at: 't', frames: [chunk(4)] }];
    replaceWindowFromRows(rows, 's1', set);
    adoptOpenTurn(get, set, 's1', rows);
    expect(get().streaming).toBeNull();
  });

  it('the adopted slot carries a controller — Stop posts /cancel like a live turn', () => {
    const { get, set } = makeStore();
    const rows = openRows([chunk(4)]);
    replaceWindowFromRows(rows, 's1', set);
    adoptOpenTurn(get, set, 's1', rows);
    expect(get().streaming?.controller).toBeTruthy();
  });
});

describe('adoptOpenTurn — the browser-WS-gap resync', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  it('re-adoption keeps the LIVE registration (its settle promise must survive)', async () => {
    const { get, set } = makeStore('s-resync');
    const o = {
      get, set,
      o: {
        sessionId: 's-resync', body: { messages: [] }, signal: new AbortController().signal,
        userParentId: null, userContent: 'hi', optimisticUserId: 'temp-1',
      },
    };
    const p = streamCompletion(o.get, o.set, o.o);
    dispatchChatFrame(get, set, 's-resync', IDS);

    // The gap: reconnect reloads the rows (open turn) and re-adopts.
    const rows = openRows([chunk(3), chunk(5)]);
    replaceWindowFromRows(rows, 's-resync', set);
    adoptOpenTurn(get, set, 's-resync', rows);
    expect(get().streaming?.messageId).toBe('am');
    expect(get().turnStartSeq).toBe(3);

    dispatchChatFrame(get, set, 's-resync', chunk(8));
    dispatchChatFrame(get, set, 's-resync', DONE);
    await p; // the ORIGINAL registration settles — no dangling await
    expect(get().streaming).toBeNull();
  });

  it('a turn that ENDED inside the gap: the stale slot closes, the pending send resolves', async () => {
    const { get, set } = makeStore('s-ended');
    const o = {
      get, set,
      o: {
        sessionId: 's-ended', body: { messages: [] }, signal: new AbortController().signal,
        userParentId: null, userContent: 'hi', optimisticUserId: 'temp-1',
      },
    };
    const p = streamCompletion(o.get, o.set, o.o);
    dispatchChatFrame(get, set, 's-ended', IDS);

    // The reload shows the turn ended (no open mark): the terminal that the
    // registration waits for died with the socket — adoption closes it.
    const rows = [{ message_id: 'am', role: 'assistant' as const, content: 'settled text', created_at: 't' }];
    replaceWindowFromRows(rows, 's-ended', set);
    adoptOpenTurn(get, set, 's-ended', rows);
    expect(get().streaming).toBeNull();
    await p; // settles instead of hanging until reload
  });
});
