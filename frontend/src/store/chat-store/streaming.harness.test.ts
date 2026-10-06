/** The ONE chat transport.
 *
 * Every chat session's turn is DRIVER-owned: POST /completions answers JSON
 * `{accepted, …}` and the frames ride the project lifecycle WS as
 * `{type:'chat_frame', session_id, frame}` (SYSTEM: chat-fanout), consumed by
 * dispatchChatFrame through the turn sink.
 * Pinned here:
 * - the POST is unconditional (there is no other transport to pick);
 * - a full turn over the dispatch path: ids → feed frames → done(content)
 *   stamps the row text but leaves the turn OPEN (done is a content frame,
 *   not a terminal); turn_closed is the ONLY terminal — it flushes the
 *   streaming slot and resolves streamCompletion;
 * - a refused POST (no frames) rejects and unregisters — late frames drop;
 * - a POST rejection that arrives AFTER the failure frames settled is
 *   swallowed (the frames already told the story);
 * - chat reset settles a pending turn (no dangling await).
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

import { streamCompletion, dispatchChatFrame, hasOpenHarnessTurn } from './streaming';
import { clearChatCaches } from './reset-registry';
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

function opts(get: () => ChatState, set: Set, sessionId = 's1') {
  return {
    get, set,
    o: {
      sessionId, body: { messages: [] }, signal: new AbortController().signal,
      userParentId: null, userContent: 'hi', optimisticUserId: 'temp-1',
    },
  };
}

/** Start a harness turn; returns the pending streamCompletion promise. */
function startHarness(get: () => ChatState, set: Set, sessionId = 's1') {
  const { o } = opts(get, set, sessionId);
  return streamCompletion(get, set, o);
}

describe('the harness transport — a turn over the WS dispatch', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  it('ids creates the rows, done stamps the content and leaves the turn open; turn_closed is terminal', async () => {
    // A fresh session id: the assembler engine is module-level, so a shared id
    // would MERGE this turn's window with the previous test's (the resumed-turn
    // merge), muddying the range assertion.
    const { get, set } = makeStore('s-win');
    const p = startHarness(get, set, 's-win');

    dispatchChatFrame(get, set, 's-win', IDS);
    expect(get().messages.map(m => m.message_id)).toEqual(['um', 'am']);
    expect(get().streaming?.messageId).toBe('am');

    dispatchChatFrame(get, set, 's-win', { type: 'dsh_event', kind: 'step/start', seq: 3, data: { turn: 0, step: 0 } });
    dispatchChatFrame(get, set, 's-win', DONE);
    // done is a CONTENT frame (the backend's text fold): the registration
    // stands and the slot keeps streaming — the turn is not closed yet.
    expect(hasOpenHarnessTurn('s-win')).toBe(true);
    expect(get().streaming).not.toBeNull();
    expect(get().messages.find(m => m.message_id === 'am')?.content).toBe('Hello world');

    // turn_closed — the only terminal — flushes the slot and resolves.
    dispatchChatFrame(get, set, 's-win', TURN_CLOSED);
    await p;
    expect(get().streaming).toBeNull();
    // The fed frame's window bound to the assistant row at the terminal.
    expect(get().turnRanges['am']).toEqual({ min: 3, max: 3 });
  });

  it('turn_closed is terminal without done (an errored turn mints no done)', async () => {
    const { get, set } = makeStore();
    const p = startHarness(get, set);
    dispatchChatFrame(get, set, 's1', IDS);
    dispatchChatFrame(get, set, 's1', { type: 'error', message: 'boom' });
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    await p;
    expect(get().streaming).toBeNull();
  });

  it('a refused POST rejects and unregisters — late frames drop', async () => {
    postMock.mockRejectedValue(new Error('409'));
    const { get, set } = makeStore();
    await expect(startHarness(get, set)).rejects.toThrow('409');
    dispatchChatFrame(get, set, 's1', IDS);
    expect(get().messages).toEqual([]);
    expect(get().streaming).toBeNull();
  });

  it('a POST rejection after the failure frames settled is swallowed', async () => {
    let rejectPost!: (e: Error) => void;
    postMock.mockReturnValue(new Promise((_res, rej) => { rejectPost = rej; }));
    const { get, set } = makeStore();
    const p = startHarness(get, set);
    dispatchChatFrame(get, set, 's1', IDS);
    dispatchChatFrame(get, set, 's1', { type: 'error', message: 'busy' });
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    rejectPost(new Error('409'));
    await p; // resolves — the frames told the story already
    expect(get().messages.find(m => m.message_id === 'am')).toBeTruthy();
  });

  it('chat reset settles a pending turn (no dangling await, registration dropped)', async () => {
    const { get, set } = makeStore('s-reset');
    const p = startHarness(get, set, 's-reset');
    dispatchChatFrame(get, set, 's-reset', IDS);
    clearChatCaches();
    await p; // settles — runCompletion's finally is reachable
    // The registration is gone: late frames find no sink. (The streaming SLOT
    // itself is flushed by the store-level reset, not by this handler.)
    dispatchChatFrame(get, set, 's-reset', DONE);
    expect(get().streaming?.messageId).toBe('am');
  });

  it('frames for a session with no open turn are ignored (another tab / reload)', () => {
    const { get, set } = makeStore();
    dispatchChatFrame(get, set, 's1', IDS);
    dispatchChatFrame(get, set, 's1', DONE);
    expect(get().messages).toEqual([]);
    expect(get().streaming).toBeNull();
  });

  it('a lore/image-gen frame with no open turn feeds the timeline for the ACTIVE session (the detached image run)', () => {
    // The run's frames ride the chat channel AFTER the turn may have ended —
    // no registration exists. A lore/image-gen frame for the active session
    // feeds the assembler (the card renders live);
    // the engine's context folds the phases, so only the settled card shows.
    const { get, set } = makeStore('s-detached');
    const anchor = { type: 'dsh_event', kind: 'tool/call', seq: 6, time: 1006,
      data: { turn: 0, step: 0, callId: 'c1', name: 'generate_image', arguments: '{}' } };
    dispatchChatFrame(get, set, 's-detached', anchor);
    dispatchChatFrame(get, set, 's-detached', {
      type: 'lore/image-gen', seq: 6.55, time: 1007, ignorable: true,
      data: { turn: 0, runId: 'r1', status: 'running', phase: 'refining' },
    });
    dispatchChatFrame(get, set, 's-detached', {
      type: 'lore/image-gen', seq: 6.6, time: 1008, ignorable: true,
      data: { turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1'] },
    });
    const conv = get().conversation;
    const gens = conv.filter(n => n.kind === 'image-gen');
    expect(gens).toHaveLength(1);
    expect(gens[0].data).toEqual({ runId: 'r1', status: 'done', imageRefIds: ['ref-1'] });
    expect(gens[0].anchorSeq).toBe(6.55); // the running frame opened the context
  });

  it('a lore/image-gen frame for a NON-active session is ignored (it shows on return through the reload)', () => {
    const { get, set } = makeStore('s-active');
    dispatchChatFrame(get, set, 's-other', {
      type: 'lore/image-gen', seq: 6.6, time: 1008, ignorable: true,
      data: { turn: 0, runId: 'r1', status: 'done', imageRefIds: ['ref-1'] },
    });
    expect(get().conversation).toEqual([]);
  });

  it('a non-lore frame with no open turn stays ignored even for the active session', () => {
    // The no-registration arm feeds ONLY lore/image-gen frames — a dsh_event
    // of another tab's turn must keep dropping (the reload owns it).
    const { get, set } = makeStore('s1');
    dispatchChatFrame(get, set, 's1', IDS);
    expect(get().messages).toEqual([]);
  });

  it("another tab's turn-bound lore frames with no open turn stay ignored for the active session", () => {
    // A tab already open on the chat has no registration while another tab
    // runs a turn (adoption happens on loadMessages only). Its verdict-ask /
    // halt / compaction-mint frames belong to that turn and must not render
    // here without it — the reload shows them with their turn.
    const { get, set } = makeStore('s1');
    const frames = [
      { type: 'lore/verdict-ask', seq: 6.5, data: { turn: 0, callId: 'c1', toolName: 'edit_document' } },
      { type: 'lore/halt', seq: 7.7, data: { turn: 0, reason: 'step_limit', steps: 3, limit: 3 } },
      { type: 'lore/compaction-mint', seq: 8.8, data: { turn: 0, compactionEntryId: 'e1', mintFailed: false } },
    ];
    for (const f of frames) {
      dispatchChatFrame(get, set, 's1', { ...f, time: 1007, ignorable: true });
    }
    expect(get().conversation).toEqual([]);
  });
});
