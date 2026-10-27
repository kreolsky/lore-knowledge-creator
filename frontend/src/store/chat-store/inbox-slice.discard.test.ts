/** ✕ on a queued chip: the removed message's text returns to the composer. */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const showToast = vi.fn();
vi.mock('../app-store', () => ({ useAppStore: { getState: () => ({ showToast }) } }));
const postMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: { post: (...a: unknown[]) => postMock(...a) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));

import { createInboxSlice } from './inbox-slice';
import type { ChatState, Set } from './types';

function makeStore(draft: string) {
  let state = {
    activeSessionId: 's1',
    draft,
    pendingInputFocus: false,
    inboxQueue: [
      { id: 'm1', text: 'first', images: 0 },
      { id: 'm2', text: 'second', images: 0 },
    ],
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  state = { ...state, setDraft: (v: string) => set({ draft: v }) } as ChatState;
  return { get, slice: createInboxSlice(set, get) };
}

describe('discardQueued', () => {
  beforeEach(() => { postMock.mockReset(); showToast.mockReset(); });

  it('returns each discarded message below the draft, in discard order', async () => {
    postMock.mockResolvedValue({ removed: true });
    const { get, slice } = makeStore('typing');
    await slice.discardQueued('m1');
    await slice.discardQueued('m2');
    expect(get().draft).toBe('typing\n\nfirst\n\nsecond');
    expect(get().pendingInputFocus).toBe(true);
    expect(postMock).toHaveBeenCalledWith('/chat/sessions/s1/inbox/discard', { message_id: 'm1' });
  });

  it('takes the text bare into an empty draft', async () => {
    postMock.mockResolvedValue({ removed: true });
    const { get, slice } = makeStore('');
    await slice.discardQueued('m2');
    expect(get().draft).toBe('second');
  });

  it('returns nothing when the message was claimed meanwhile', async () => {
    postMock.mockResolvedValue({ removed: false });
    const { get, slice } = makeStore('typing');
    await slice.discardQueued('m1');
    expect(get().draft).toBe('typing');
    expect(get().pendingInputFocus).toBe(false);
  });

  it('keeps the draft and toasts when the discard fails', async () => {
    postMock.mockRejectedValue(new Error('502'));
    const { get, slice } = makeStore('typing');
    await slice.discardQueued('m1');
    expect(get().draft).toBe('typing');
    expect(showToast).toHaveBeenCalledWith('queueDiscardFailed', 'error');
  });
});
