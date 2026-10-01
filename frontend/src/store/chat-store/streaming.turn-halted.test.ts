import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock the app store (currentUser + showToast) and the API client's post()
// (the harness transport's only HTTP arm).
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

// Minimal zustand-like get/set over a mutable ChatState.
function makeStore() {
  let state = {
    sessions: [], messages: [], selectedSiblings: {}, streaming: null,
    conversation: [], turnRanges: {}, turnStartSeq: null,
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

async function drive(frames: Record<string, unknown>[]) {
  const { get, set } = makeStore();
  const p = streamCompletion(get, set, {
    sessionId: 's1', body: {}, signal: new AbortController().signal,
    userParentId: null, userContent: 'hi',
  });
  dispatchChatFrame(get, set, 's1', IDS);
  for (const f of frames) dispatchChatFrame(get, set, 's1', f);
  dispatchChatFrame(get, set, 's1', TURN_CLOSED);
  await p;
  return { get, msg: get().messages.find(m => m.message_id === 'am')! };
}

/**
 * The halt CARD and the turn's timeline are the assembler's nodes; the live
 * arms beside the feed owe the error notice + toast, and the end-reason stamp
 * the message queue reads at the terminal (queue-slice.test.ts).
 */
describe('halt and error frames — what the live path still owes', () => {
  beforeEach(() => { vi.clearAllMocks(); postMock.mockResolvedValue({ accepted: true }); });

  it('a lore/halt frame rides the feed as the halt card', async () => {
    const { get } = await drive([
      { type: 'lore/halt', seq: 5, data: { reason: 'tool_call_limit', turn: 0 } },
    ]);
    expect(get().conversation.some(n => n.kind === 'halt')).toBe(true);
  });

  it('an error frame toasts and appends the notice to content', async () => {
    const { msg } = await drive([
      { type: 'error', message: 'boom' },
    ]);
    // The toast is unconditional: an assembled turn renders its NODES instead
    // of `content`, so the appended notice alone can go unread.
    expect(showToast).toHaveBeenCalledWith('chatSendFailed', 'error');
    expect(msg.content).toContain('boom');
  });
});
