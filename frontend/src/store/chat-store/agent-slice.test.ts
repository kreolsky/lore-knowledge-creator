/** Agent slice — the reload re-render half of mid-turn approval: a still-held
 * call is rediscovered from GET /chat/verdicts and merged onto the assistant
 * message's pending_verdicts, where VerdictCard renders it. The live frame path
 * (awaiting_verdict) lives in streaming.tool-prepare.test.ts; this is the
 * reload path. */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const showToast = vi.fn();
const { getMock, postMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
}));
vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ showToast }) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
vi.mock('../../api/client', () => ({
  apiClient: { get: getMock, post: postMock },
  HttpError: class HttpError extends Error {
    constructor(public status: number, message?: string) { super(message ?? String(status)); }
  },
}));

import { createAgentSlice } from './agent-slice';
import type { ChatState, Set } from './types';

function makeStore(initialMessages: unknown[], activeSessionId = 's1') {
  let state = { messages: initialMessages, activeSessionId } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  // Composed-store shape: the slice's own methods are reachable through get()
  // (decideVerdict calls get().removePendingVerdictByCall).
  const slice = createAgentSlice(set, get);
  state = { ...state, ...slice } as unknown as ChatState;
  return { get, set };
}

const MSG = {
  message_id: 'am',
  role: 'assistant',
  content: 'reply',
  pending_verdicts: [],
};

beforeEach(() => { showToast.mockClear(); });

describe('fetchPendingVerdicts — the reload re-render path', () => {
  it('merges held calls onto their assistant message pending_verdicts (cards render after reload)', async () => {
    const { get, set } = makeStore([{ ...MSG }]);
    getMock.mockResolvedValue({
      holds: [
        { call_id: 'c1', tool_name: 'edit_document', message_id: 'am' },
        { call_id: 'c2', tool_name: 'append_to_document', message_id: 'am' },
      ],
    } as never);

    const slice = createAgentSlice(set, get);
    await slice.fetchPendingVerdicts('s1');

    const messages = get().messages as Array<{ message_id: string; pending_verdicts: unknown[] }>;
    expect(messages[0].pending_verdicts).toEqual([
      { call_id: 'c1', tool_name: 'edit_document', message_id: 'am' },
      { call_id: 'c2', tool_name: 'append_to_document', message_id: 'am' },
    ]);
  });

  it('never duplicates an already-present hold (frame card + reload race)', async () => {
    const { get, set } = makeStore([{ ...MSG, pending_verdicts: [{ call_id: 'c1', tool_name: 'edit_document', message_id: 'am' }] }]);
    getMock.mockResolvedValue({
      holds: [
        { call_id: 'c1', tool_name: 'edit_document', message_id: 'am' },
        { call_id: 'c2', tool_name: 'append_to_document', message_id: 'am' },
      ],
    } as never);

    const slice = createAgentSlice(set, get);
    await slice.fetchPendingVerdicts('s1');

    const messages = get().messages as Array<{ pending_verdicts: Array<{ call_id: string }> }>;
    expect(messages[0].pending_verdicts).toHaveLength(2);
    expect(messages[0].pending_verdicts.map(p => p.call_id)).toEqual(['c1', 'c2']);
  });

  it('holds without a message_id are dropped (no card can attach to a message)', async () => {
    const { get, set } = makeStore([{ ...MSG }]);
    getMock.mockResolvedValue({
      holds: [{ call_id: 'c1', tool_name: 'edit_document', message_id: '' }],
    } as never);

    const slice = createAgentSlice(set, get);
    await slice.fetchPendingVerdicts('s1');

    const messages = get().messages as Array<{ pending_verdicts: unknown[] }>;
    expect(messages[0].pending_verdicts).toEqual([]);
  });
});

describe('decideVerdict — expired hold (409 hold_expired)', () => {
  const held = [
    { ...MSG, pending_verdicts: [{ call_id: 'c9', tool_name: 'edit_document', message_id: 'am' }] },
  ];

  it('409 removes the card and toasts the explicit expiry reason (not the generic failure)', async () => {
    const { HttpError } = await import('../../api/client');
    postMock.mockRejectedValueOnce(new HttpError(409, 'hold_expired'));
    const { get, set } = makeStore(held);

    const slice = createAgentSlice(set, get);
    await slice.decideVerdict('c9', 'allow_once', 'edit_document');

    const messages = get().messages as Array<{ pending_verdicts: unknown[] }>;
    expect(messages[0].pending_verdicts).toEqual([]);
    expect(showToast).toHaveBeenCalledWith('verdictExpired', 'warning');
    expect(showToast).not.toHaveBeenCalledWith('verdictFailed', 'error');
  });

  it('404 still clears the stale card silently (unchanged behavior)', async () => {
    const { HttpError } = await import('../../api/client');
    postMock.mockRejectedValueOnce(new HttpError(404, 'no held call'));
    const { get, set } = makeStore(held);

    const slice = createAgentSlice(set, get);
    await slice.decideVerdict('c9', 'allow_once', 'edit_document');

    const messages = get().messages as Array<{ pending_verdicts: unknown[] }>;
    expect(messages[0].pending_verdicts).toEqual([]);
    expect(showToast).not.toHaveBeenCalled();
  });
});

