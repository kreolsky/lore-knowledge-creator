/** dispatchChatFrame's NO-TURN malformed-lore arm (the detached run).
 *
 * A detached lore/image-gen run has no turn registration, so no per-turn sink
 * exists to hang a one-shot on — the malformed drop keeps its own per-session
 * one-shot (one chatStreamDegraded toast per session, cleared on chat reset).
 * Pinned here:
 * - a malformed no-turn lore frame toasts ONCE per session, never per frame;
 * - a chat reset (project switch / logout) re-arms it;
 * - a frame for a session that is not shown never toasts.
 *
 * Test seam: the production GUARDS (frame-validate.ts) carry no
 * lore/image-gen entry, so validateFrame cannot reject a well-typed lore frame
 * today — the arm is exercised through a file-scoped validateFrame mock that
 * rejects exactly the no-turn lore frame, pinning the drop's contract for the
 * day a guard lands.
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock('../app-store', () => {
  const state = {
    currentUser: { user_id: 'u1', name: 'U' },
    showToast: vi.fn(),
  };
  return { useAppStore: { getState: vi.fn(() => state), subscribe: () => () => {} } };
});
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
vi.mock('./frame-validate', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./frame-validate')>();
  return {
    ...actual,
    validateFrame: (obj: unknown) =>
      (obj as { type?: unknown } | null)?.type === 'lore/image-gen'
        ? null
        : actual.validateFrame(obj),
  };
});

import { dispatchChatFrame } from './streaming';
import { clearChatCaches } from './reset-registry';
import { useAppStore } from '../app-store';
import type { ChatState, Set } from './types';

function makeStore(sessionId = 's1') {
  let state = {
    activeSessionId: sessionId,
    sessions: [{
      session_id: sessionId, document_id: null, reference_id: null, user_id: 'u1',
    }],
    messages: [],
    streaming: null,
    conversation: [],
    turnRanges: {},
    turnStartSeq: null,
    inboxQueue: [],
    dshRunning: false,
  } as unknown as ChatState;
  const get = () => state;
  const set: Set = (u: unknown) => {
    const patch = typeof u === 'function' ? (u as (s: ChatState) => Partial<ChatState>)(state) : u;
    state = { ...state, ...(patch as Partial<ChatState>) };
  };
  return { get, set };
}

const toast = () => useAppStore.getState().showToast as unknown as ReturnType<typeof vi.fn>;

describe('dispatchChatFrame — malformed DETACHED lore frame (no turn)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('toasts once per session, never per frame; a chat reset re-arms it', () => {
    const { get, set } = makeStore('s-lore'); // active, nothing registered
    const BAD = { type: 'lore/image-gen', mint: {} };

    dispatchChatFrame(get, set, 's-lore', BAD);
    expect(toast()).toHaveBeenCalledTimes(1);
    expect(toast()).toHaveBeenCalledWith('chatStreamDegraded', 'warning');

    dispatchChatFrame(get, set, 's-lore', BAD);
    expect(toast()).toHaveBeenCalledTimes(1); // one-shot per session

    // Project switch / logout — the module's per-session state clears with it.
    clearChatCaches();
    dispatchChatFrame(get, set, 's-lore', BAD);
    expect(toast()).toHaveBeenCalledTimes(2); // re-armed
  });

  it('never toasts for a session that is not shown', () => {
    const { get, set } = makeStore('s-shown');
    dispatchChatFrame(get, set, 's-idle', { type: 'lore/image-gen' });
    expect(toast()).not.toHaveBeenCalled();
  });
});
