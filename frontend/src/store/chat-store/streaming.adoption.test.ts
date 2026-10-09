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
    queued: {},
    flushQueued: async () => {},
    restoreQueued: () => {},
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
    // turn renders in the streaming slot.
    expect(get().turnStartSeq).toBe(4);

    dispatchChatFrame(get, set, 's1', chunk(7));
    dispatchChatFrame(get, set, 's1', DONE);
    // done is a content frame — the adopted turn closes on its terminal.
    dispatchChatFrame(get, set, 's1', { type: 'turn_closed' });
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

  it('leave mid-turn and return: the slot re-seats for the chat and the turn settles', async () => {
    const { get, set } = makeStore('s-back');
    // The turn starts while the chat is shown.
    const p = streamCompletion(get, set, {
      sessionId: 's-back', body: {}, signal: new AbortController().signal,
      userParentId: null, userContent: 'hi', optimisticUserId: 'temp-1',
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

describe('adoptOpenTurn — the reload keeps the streamed text', () => {
  // The open row carries the plugin's fold of the live stream
  // (`assistant_stream`, dsh's accumulator snapshot) — adoption seats the
  // attempt and replays its compact records as live-chunk entries, so the
  // already-streamed text is on screen immediately and the next live chunk
  // continues the same band series.
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  const BASELINE = {
    revision: 2,
    activeAttempt: {
      attemptId: 'at1', startedAfterSeq: 5, turn: 1, step: 1, nextIndex: 3,
      stream: [{ type: 'text-chunks', time0: 2000, index: 0, dt: [1, 1], texts: ['Hel', 'lo w', 'orld'] }],
    },
  };
  /** The open turn's durable frames: turn/start + step/start — the v4 log
   * holds only settled events, so a mid-STEP reload has no assistant/message
   * row for the streaming step yet. */
  const openTurnFrames = () => [
    { type: 'dsh_event', kind: 'turn/start', seq: 4, time: 1004, data: { turn: 1 } },
    { type: 'dsh_event', kind: 'step/start', seq: 5, time: 1005, data: { turn: 1, step: 1 } },
  ];
  const baselineRows = () => [{
    message_id: 'am', chat_id: 's1', parent_id: 'um', role: 'assistant' as const,
    content: '', created_at: '2026-01-01', open_turn: true,
    frames: openTurnFrames(), assistant_stream: BASELINE,
  }];
  const streamedText = (state: ChatState): string => {
    const node = state.conversation.find(n => n.kind === 'assistant-step');
    const blocks = (node?.data as { blocks?: { kind: string; text?: string }[] } | undefined)?.blocks ?? [];
    return blocks.filter(b => b.kind === 'text').map(b => b.text ?? '').join('');
  };

  it('adoption with a baseline renders its text before any live chunk', () => {
    const { get, set } = makeStore();
    const rows = baselineRows();
    replaceWindowFromRows(rows, 's1', set);
    adoptOpenTurn(get, set, 's1', rows);
    set({ messages: rows.map(stripFrames) as ChatState['messages'] });
    __flushFeedPublishForTest(set);

    expect(get().streaming?.messageId).toBe('am');
    expect(streamedText(get())).toBe('Hello world');
    // The wire key never enters the store.
    expect(get().messages.find(m => m.message_id === 'am')).not.toHaveProperty('assistant_stream');
  });

  it('the next live chunk continues at nextIndex; an abandoned end leaves no ghost', () => {
    const { get, set } = makeStore();
    const rows = baselineRows();
    replaceWindowFromRows(rows, 's1', set);
    adoptOpenTurn(get, set, 's1', rows);
    __flushFeedPublishForTest(set);

    // Chunk 4 must land at the band's k=4 position — above the three adopted
    // entries. Had adoption not advanced the gap counter, this chunk would
    // collide with the first adopted seq (append answers 'none' on a held
    // seq) and the text would never grow.
    dispatchChatFrame(get, set, 's1', {
      type: 'dsh_stream',
      frame: { type: 'chunk', attemptId: 'at1', revision: 2, index: 3, time: 2003, chunk: { type: 'text-delta', index: 0, text: '!' } },
    });
    __flushFeedPublishForTest(set);
    expect(streamedText(get())).toBe('Hello world!');

    // The attempt's end retires the ADOPTED transient rows too — an abandoned
    // attempt leaves no ghost text across the reload boundary.
    dispatchChatFrame(get, set, 's1', {
      type: 'dsh_stream',
      frame: { type: 'end', attemptId: 'at1', revision: 2, index: 4, outcome: { kind: 'abandoned' } },
    });
    __flushFeedPublishForTest(set);
    expect(streamedText(get())).toBe('');
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
    dispatchChatFrame(get, set, 's-resync', { type: 'turn_closed' });
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
