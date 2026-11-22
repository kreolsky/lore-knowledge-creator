/** Agent slice — decideVerdict's answer to a hold that is already gone: the
 * card folds as stale, and only the owned expiry (409) says why. */
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
  const slice = createAgentSlice(set, get);
  state = { ...state, ...slice } as unknown as ChatState;
  return { get, set };
}

beforeEach(() => { showToast.mockClear(); });

describe('decideVerdict — expired hold (409 hold_expired)', () => {
  it('409 folds the card as stale and toasts the explicit expiry reason (not the generic failure)', async () => {
    const { HttpError } = await import('../../api/client');
    postMock.mockRejectedValueOnce(new HttpError(409, 'hold_expired'));
    const { get, set } = makeStore([]);

    const slice = createAgentSlice(set, get);
    expect(await slice.decideVerdict('c9', 'allow_once', 'edit_document')).toBe('stale');

    expect(showToast).toHaveBeenCalledWith('verdictExpired', 'warning');
    expect(showToast).not.toHaveBeenCalledWith('verdictFailed', 'error');
  });

  it('404 folds the stale card silently', async () => {
    const { HttpError } = await import('../../api/client');
    postMock.mockRejectedValueOnce(new HttpError(404, 'no held call'));
    const { get, set } = makeStore([]);

    const slice = createAgentSlice(set, get);
    expect(await slice.decideVerdict('c9', 'allow_once', 'edit_document')).toBe('stale');

    expect(showToast).not.toHaveBeenCalled();
  });
});

