import { describe, it, expect, vi, beforeEach } from 'vitest';

// The verbatim dsh_event + lore/*
// frames are the BROWSER assembler's input. The harness dispatch feeds them to
// conversation-feed (append for the tail, splice for the mints) and the store
// carries the PUBLISHED node list — no neutral-chip fold.
// These tests drive the WS dispatch (register + POST, frames, terminal) and
// read the published conversation.

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
import { clearFeed, __flushFeedPublishForTest } from './conversation-feed';
import type { ChatState, Set } from './types';

function makeStore(initial: Record<string, unknown> = {}) {
  let state = {
    // The turn's chat is the SHOWN one — display work (feed, rows, slot) is
    // gated on this pointer (see the isShown gate in streaming.ts).
    activeSessionId: 's1',
    sessions: [], messages: [], selectedSiblings: {}, streaming: null,
    conversation: [], turnRanges: {}, turnStartSeq: null, ...initial,
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  return { get, set };
}

const IDS = { type: 'ids', user_message_id: 'um', assistant_message_id: 'am' };
const TURN_CLOSED = { type: 'turn_closed' };

async function drive(frames: Record<string, unknown>[], initial: Record<string, unknown> = {}) {
  const { get, set } = makeStore(initial);
  const p = streamCompletion(get, set, {
    sessionId: 's1', body: {}, signal: new AbortController().signal,
    userParentId: null, userContent: 'hi',
  });
  dispatchChatFrame(get, set, 's1', IDS);
  for (const f of frames) dispatchChatFrame(get, set, 's1', f);
  dispatchChatFrame(get, set, 's1', TURN_CLOSED);
  await p;
  // Live-chunk publications coalesce per animation frame (jsdom: a 16ms
  // timeout) — flush before the assertions read the store.
  __flushFeedPublishForTest(set);
  return get;
}

const USER_MSG = (seq: number, id: string) => ({
  type: 'dsh_event', kind: 'user/message', seq, time: 1000 + seq,
  data: { id, content: [{ type: 'text', text: 'hi' }], source: { kind: 'user' } },
  surfaceOp: 'append',
});
/** A settled assistant step (the v4 log holds only settled events:
 * `assistant/message` carries the whole
 * text; deltas never enter the log — the live tail rides `dsh_stream`). */
const ASSISTANT = (seq: number, text: string) => ({
  type: 'dsh_event', kind: 'assistant/message', seq, time: 1000 + seq,
  data: { turn: 0, step: 0, message: { id: `m${seq}`, role: 'assistant', content: [{ type: 'text', text }], source: { kind: 'model', provider: 'lore', model: 'test' } }, stream: [] },
  surfaceOp: 'append',
});

describe('dsh_event frames feed the assembler', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    postMock.mockResolvedValue({ accepted: true });
    clearFeed(vi.fn());
  });

  it('publishes the assembled nodes onto the store', async () => {
    const get = await drive([
      USER_MSG(1, 'u1'),
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 2.5, time: 1002, data: { turn: 0, step: 0 } },
      ASSISTANT(3, 'Hello'),
      { type: 'dsh_event', kind: 'tool/call', seq: 4, time: 1004, data: { turn: 0, step: 0, callId: 'c1', name: 'search', arguments: '{}' } },
      { type: 'dsh_event', kind: 'tool/result', seq: 5, time: 1005, data: { turn: 0, step: 0, message: { content: [{ type: 'text', text: 'found', isError: false }], source: { callId: 'c1' } } } },
      { type: 'dsh_event', kind: 'turn/end', seq: 6, time: 1006, data: { turn: 0, reason: { kind: 'completed' } } },
    ]);
    const kinds = get().conversation.map(n => n.kind);
    expect(kinds).toContain('user');
    expect(kinds).toContain('assistant-step');
    expect(kinds).toContain('tool-call');
  });

  it('keeps the live turn boundary: nodes above turnStartSeq bind to the row at flush', async () => {
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      ASSISTANT(3, 'Hi'),
      { type: 'dsh_event', kind: 'turn/end', seq: 4, time: 1004, data: { turn: 0, reason: { kind: 'completed' } } },
    ]);
    // The terminal frame closed the turn: the window is bound to the row.
    expect(get().turnRanges.am).toEqual({ min: 2, max: 4 });
    expect(get().turnStartSeq).toBeNull();
  });

  it('splices a lore mint into the published conversation', async () => {
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'turn/end', seq: 3, time: 1003, data: { turn: 0, reason: { kind: 'aborted', reason: { kind: 'user' } } } },
      { type: 'lore/halt', seq: 3.7, time: 1004, data: { turn: 0, reason: 'aborted' }, ignorable: true },
    ]);
    const halt = get().conversation.find(n => n.kind === 'halt');
    expect(halt).toBeDefined();
    expect((halt!.data as Record<string, unknown>).reason).toBe('aborted');
  });

  it('toasts the compaction mint outcome beside the feed', async () => {
    await drive([
      { type: 'lore/compaction-mint', seq: 9.8, time: 1009, data: { turn: 0, compactionEntryId: 'w1', mintFailed: false }, ignorable: true },
    ]);
    expect(showToast).toHaveBeenCalledWith('chatContextSummarized', 'info');
    showToast.mockClear();
    await drive([
      { type: 'lore/compaction-mint', seq: 11.8, time: 1011, data: { turn: 0, compactionEntryId: 'w2', mintFailed: true, mintReason: 'boom' }, ignorable: true },
    ]);
    expect(showToast).toHaveBeenCalledWith('chatCompactionArchiveFailed', 'warning');
  });

  it('ignores non-feed frames (unknown forward-compat types pass through silently)', async () => {
    const get = await drive([{ type: 'model_update', model: 'm1' }]);
    expect(get().conversation).toEqual([]);
  });
});

// The live tail rides `dsh_stream` (the v4 log holds only settled events):
// the plugin taps dsh's own `agent/assistant-stream` and relays its frames
// verbatim; the feed maps each CHUNK to the assembler's transient
// `assistant/live-chunk` entry, fractionally ordered above the durable tail
// (dsh's own ClientAssistantStream seq formula), so the text streams BEFORE
// the settled `assistant/message` lands. The START frame carries the
// attempt's turn/step (a chunk does not) — no open attempt, no entry.
const STREAM_START = {
  type: 'dsh_stream',
  frame: { type: 'start', attemptId: 'a1', revision: 1, turn: 0, step: 0 },
};
const STREAM_CHUNK = (index: number, text: string) => ({
  type: 'dsh_stream',
  frame: {
    type: 'chunk', attemptId: 'a1', revision: 1, index,
    time: 2000 + index, chunk: { type: 'text-delta', index: 0, text },
  },
});
const STREAM_END = {
  type: 'dsh_stream',
  frame: {
    type: 'end', attemptId: 'a1', revision: 1, index: 2,
    outcome: { kind: 'committed', eventType: 'assistant/message', seq: 4 },
  },
};

function streamedText(nodes: { kind: string; data: unknown }[]): string {
  const node = nodes.find(n => n.kind === 'assistant-step');
  const blocks = (node?.data as { blocks?: { kind: string; text?: string }[] } | undefined)?.blocks ?? [];
  return blocks.filter(b => b.kind === 'text').map(b => b.text ?? '').join('');
}

describe('dsh_stream frames advance the assistant node live', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    postMock.mockResolvedValue({ accepted: true });
    clearFeed(vi.fn());
  });

  it('streams chunk text onto the assistant step before any settlement', async () => {
    const get = await drive([
      USER_MSG(1, 'u1'),
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 3, time: 1003, data: { turn: 0, step: 0 } },
      STREAM_START,
      STREAM_CHUNK(0, 'Hel'),
      STREAM_CHUNK(1, 'lo'),
    ]);
    // No assistant/message was fed: the transient chunks alone hold the text.
    expect(streamedText(get().conversation)).toBe('Hello');
  });

  it('the settled assistant/message supersedes the transient rows without a duplicate node', async () => {
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 3, time: 1003, data: { turn: 0, step: 0 } },
      STREAM_START,
      STREAM_CHUNK(0, 'Hel'),
      STREAM_CHUNK(1, 'lo'),
      STREAM_END,
      ASSISTANT(4, 'Hello world'),
      { type: 'dsh_event', kind: 'turn/end', seq: 5, time: 1005, data: { turn: 0, reason: { kind: 'completed' } } },
    ]);
    const steps = get().conversation.filter(n => n.kind === 'assistant-step');
    expect(steps).toHaveLength(1);
    expect(streamedText(get().conversation)).toBe('Hello world');
  });

  it('a chunk with no open attempt adds nothing — the settlement renders the text', async () => {
    // Mid-attempt subscribe: the start frame never arrived, so the chunk's
    // turn/step is unknown and the feed ignores it (dsh's own fold: the
    // durable settlement publishes directly).
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 3, time: 1003, data: { turn: 0, step: 0 } },
      STREAM_CHUNK(0, 'x'),
      ASSISTANT(4, 'x'),
    ]);
    expect(streamedText(get().conversation)).toBe('x');
  });

  it.each([0.5, 0.8])('chunk seqs never collide with a lore mint at tail + %s', async (offset) => {
    // The bare k/(k+1) series hits 0.5 / 0.8 at k=1 / k=4 — the verdictAsk
    // and compactionMint offsets; the assembler drops a second arrival of a
    // seq it holds, so a collision would lose a card or a delta. Chunks fed
    // BEFORE a mint on the same integral tail: the chunks and the mint must
    // both survive (a halt stands in for any mint — one node per turn).
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 3, time: 1003, data: { turn: 0, step: 0 } },
      STREAM_START,
      STREAM_CHUNK(0, 'a'), STREAM_CHUNK(1, 'b'), STREAM_CHUNK(2, 'c'),
      STREAM_CHUNK(3, 'd'), STREAM_CHUNK(4, 'e'),
      { type: 'lore/halt', seq: 3 + offset, time: 1004, data: { turn: 0, reason: 'aborted' }, ignorable: true },
    ]);
    expect(streamedText(get().conversation)).toBe('abcde');
    expect(get().conversation.filter(n => n.kind === 'halt')).toHaveLength(1);
  });

  it('start and end frames publish no text of their own', async () => {
    const get = await drive([
      { type: 'dsh_event', kind: 'turn/start', seq: 2, time: 1002, data: { turn: 0 } },
      { type: 'dsh_event', kind: 'step/start', seq: 3, time: 1003, data: { turn: 0, step: 0 } },
      STREAM_START,
      STREAM_END,
    ]);
    expect(streamedText(get().conversation)).toBe('');
  });
});

// The harness titler's `session_title` frame is the title's transport — the
// store applies it to the chat-list row live (the list acceptance: "without a
// second request"). The empty and tokenizer-leak payloads are guarded at the
// SOURCE (harness-driver/plugin/src/map.ts), so what arrives here is a title
// worth showing; the user pin mirrors the backend write's guard
// (turn_persistence._persist_session_title). The frame records no step.
describe('session_title applies to the sessions list', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  const row = (over: Record<string, unknown> = {}) => ({
    session_id: 's1', title: 'Old', ...over,
  });
  const titleFrame = (title: unknown) => ({ type: 'session_title', title });

  it('applies the title to the current session row and mints no step', async () => {
    const get = await drive(
      [titleFrame('Fresh Title')],
      { sessions: [row(), { session_id: 'other', title: 'Untouched' }] },
    );
    expect(get().sessions.find(x => x.session_id === 's1')?.title).toBe('Fresh Title');
    expect(get().sessions.find(x => x.session_id === 'other')?.title).toBe('Untouched');
  });

  it('does not clobber a user-pinned title', async () => {
    const get = await drive(
      [titleFrame('Revision')],
      { sessions: [row({ title: 'Mine', title_user_set: true })] },
    );
    expect(get().sessions.find(x => x.session_id === 's1')?.title).toBe('Mine');
  });

  it('ignores a frame with no usable title', async () => {
    for (const title of ['', undefined, null, 42]) {
      const get = await drive([titleFrame(title)], { sessions: [row()] });
      expect(get().sessions.find(x => x.session_id === 's1')?.title).toBe('Old');
    }
  });
});
