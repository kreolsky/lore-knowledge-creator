/** The queue is dsh's inbox: what the browser does with a claim it did not
 * send live (`ids` with no registration), with the splice/status frames, and
 * with a user row matched by the browser-minted user_msg_id. */

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

import {
  streamCompletion, dispatchChatFrame, hasOpenHarnessTurn, rememberQueuedSend,
} from './streaming';
import { resetInbox, seedInbox } from './inbox-slice';
import { insertAndPinSession } from './session-helpers';
import { clearChatCaches } from './reset-registry';
import type { ChatMessage } from '../../types';
import type { ChatState, Set } from './types';

function makeStore(active = 's1', messages: ChatMessage[] = []) {
  let state = {
    activeSessionId: active,
    sessions: [], messages, streaming: null,
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

const row = (id: string, parent: string | null, role: 'user' | 'assistant', at: string): ChatMessage =>
  ({ message_id: id, chat_id: 's1', parent_id: parent, role, content: id, created_at: at }) as ChatMessage;
const HISTORY = [row('u0', null, 'user', '2026-01-01T00:00:00Z'), row('a0', 'u0', 'assistant', '2026-01-01T00:00:01Z')];

const ids = (user: string, assistant: string, text = '', chat = 's1') =>
  ({ type: 'ids', user_message_id: user, assistant_message_id: assistant, user_text: text, chat_id: chat });
const TURN_CLOSED = { type: 'turn_closed' };
const splice = (seq: number, start: number, removedCount: number, inserted: unknown[] = []) => ({
  type: 'dsh_event', kind: 'agent/inbox/spliced', seq,
  data: { target: 'next-turn', start, removedCount, inserted },
});
const dshMsg = (id: string, text: string) => ({ id, role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] });

describe('a claimed queued message opens its own turn', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearChatCaches();
    postMock.mockResolvedValue({ accepted: true });
  });

  it('ids with no registration in the ACTIVE chat seats the slot and the pair, on the tail', () => {
    const { get, set } = makeStore('s1', HISTORY);
    dispatchChatFrame(get, set, 's1', ids('uq', 'aq', 'queued text'));
    expect(hasOpenHarnessTurn('s1')).toBe(true);
    expect(get().streaming?.messageId).toBe('aq');
    const user = get().messages.find(m => m.message_id === 'uq')!;
    expect(user.content).toBe('queued text');
    // The claim parents on the chat's tail at claim — the last assistant.
    expect(user.parent_id).toBe('a0');
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    expect(hasOpenHarnessTurn('s1')).toBe(false);
    expect(get().streaming).toBeNull();
  });

  it('a claim minted into ANOTHER chat (a compaction continuation) opens nothing here', () => {
    // The source chat's fan-out still carries its driver session's frames, but
    // the pair lands in the continuation: rendering it here would show a turn
    // that a reload removes.
    const { get, set } = makeStore('s1', HISTORY);
    dispatchChatFrame(get, set, 's1', ids('uq', 'aq', 'elsewhere', 'cont'));
    expect(hasOpenHarnessTurn('s1')).toBe(false);
    expect(get().messages.some(m => m.message_id === 'uq')).toBe(false);
    expect(get().streaming).toBeNull();
  });

  it("this tab's own queued send brings its text AND images to the bubble", () => {
    const { get, set } = makeStore('s1', HISTORY);
    rememberQueuedSend('uq', 'mine', ['data:image/png;base64,AAA']);
    dispatchChatFrame(get, set, 's1', ids('uq', 'aq', 'mine'));
    const user = get().messages.find(m => m.message_id === 'uq')!;
    expect(user.images).toEqual(['data:image/png;base64,AAA']);
  });

  it('a queue of two renders as two consecutive turns, each its own pair', () => {
    const { get, set } = makeStore('s1', HISTORY);
    dispatchChatFrame(get, set, 's1', ids('u1', 'a1', 'one'));
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    dispatchChatFrame(get, set, 's1', ids('u2', 'a2', 'two'));
    expect(get().streaming?.messageId).toBe('a2');
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    const byId = Object.fromEntries(get().messages.map(m => [m.message_id, m.parent_id]));
    expect(byId).toMatchObject({ u1: 'a0', a1: 'u1', u2: 'a1', a2: 'u2' });
  });

  it('ids for a chat that is not shown opens nothing (the reload brings it)', () => {
    const { get, set } = makeStore('other');
    dispatchChatFrame(get, set, 's1', ids('uq', 'aq', 'x'));
    expect(hasOpenHarnessTurn('s1')).toBe(false);
    expect(get().messages).toEqual([]);
  });

  it("an idle send's registration replaces ONLY its own row — a claim ahead of it is a new row", async () => {
    // The idle judgement was stale: another tab's queued message runs first.
    const optimistic = row('mine', 'a0', 'user', '2026-01-01T00:00:02Z');
    const { get, set } = makeStore('s1', [...HISTORY, optimistic]);
    const p = streamCompletion(get, set, { sessionId: 's1', body: {}, signal: new AbortController().signal });
    dispatchChatFrame(get, set, 's1', ids('foreign', 'af', 'from the other tab'));
    expect(get().messages.find(m => m.message_id === 'mine')?.content).toBe('mine');
    expect(get().messages.find(m => m.message_id === 'foreign')?.content).toBe('from the other tab');
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
    await p;
    // Our own claim follows: its ids replaces the optimistic row in place.
    dispatchChatFrame(get, set, 's1', ids('mine', 'am', 'mine'));
    expect(get().messages.filter(m => m.message_id === 'mine')).toHaveLength(1);
    expect(get().messages.find(m => m.message_id === 'am')?.parent_id).toBe('mine');
    // …and re-parented onto the tail its claim found (the foreign answer).
    expect(get().messages.find(m => m.message_id === 'mine')?.parent_id).toBe('af');
    dispatchChatFrame(get, set, 's1', TURN_CLOSED);
  });
});

describe('inbox and status frames fold for the active chat only', () => {
  beforeEach(() => { clearChatCaches(); });

  it('splices move the queue on top of the snapshot; dsh_status sets the busy state', () => {
    const { get, set } = makeStore('s1');
    resetInbox(set);
    seedInbox(set, [dshMsg('m1', 'first')], 10);
    dispatchChatFrame(get, set, 's1', splice(11, 1, 0, [dshMsg('m2', 'second')]));
    expect(get().inboxQueue.map(m => m.text)).toEqual(['first', 'second']);
    dispatchChatFrame(get, set, 's1', { type: 'dsh_status', status: 'running' });
    expect(get().dshRunning).toBe(true);
    // The claim of the head removes its chip.
    dispatchChatFrame(get, set, 's1', splice(12, 0, 1));
    expect(get().inboxQueue.map(m => m.id)).toEqual(['m2']);
    dispatchChatFrame(get, set, 's1', { type: 'dsh_status', status: 'idle' });
    expect(get().dshRunning).toBe(false);
  });

  it("another chat's splice or status never touches the shown queue", () => {
    const { get, set } = makeStore('s1');
    resetInbox(set);
    seedInbox(set, [], 0);
    dispatchChatFrame(get, set, 'other', splice(1, 0, 0, [dshMsg('x', 'X')]));
    dispatchChatFrame(get, set, 'other', { type: 'dsh_status', status: 'running' });
    expect(get().inboxQueue).toEqual([]);
    expect(get().dshRunning).toBe(false);
  });

  it('a chat this tab just created shows its queue — no load ever seeded it', () => {
    // A created chat is activated without a messages GET; its inbox is known
    // empty (no dsh agent yet), so the first busy send's splice must render.
    const { get, set } = makeStore(null as unknown as string);
    resetInbox(set);
    insertAndPinSession(set, { session_id: 'fresh' } as never, {});
    dispatchChatFrame(get, set, 'fresh', splice(5, 0, 0, [dshMsg('m1', 'queued')]));
    expect(get().inboxQueue.map(m => m.text)).toEqual(['queued']);
  });
});
