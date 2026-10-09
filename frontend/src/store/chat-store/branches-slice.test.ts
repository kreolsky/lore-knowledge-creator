/** Branch slice openBranch — the thread ROOT resolution for the switcher.
 *
 * The review card on step 2 (plan chat-branch-sessions): a switcher target
 * that is an unlisted sibling (no row in `sessions` — the thread's list row
 * previews only the last-OPENED branch) fell back to patching the BRANCH
 * itself; the guard accepts it (same thread) and the chat list's preview
 * silently stopped following the switch. The root must resolve from the
 * ACTIVE session's thread, the target's row only as a fork-flow fallback,
 * and a remembered branch→root fact covers switches between TWO unlisted
 * siblings (neither side has a row). */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

const showToast = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    patch: vi.fn(),
  },
}));
vi.mock('../app-store', () => ({
  useAppStore: { getState: () => ({ showToast }) },
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));

import { createBranchesSlice } from './branches-slice';
import { apiClient } from '../../api/client';
import type { ChatState } from './types';

function sess(id: string, threadId: string | null): ChatState['sessions'][number] {
  return {
    session_id: id,
    thread_id: threadId,
  } as unknown as ChatState['sessions'][number];
}

/** A minimal store: the slice + the fields openBranch reads/writes. */
function buildStore(sessions: ChatState['sessions'], activeSessionId: string | null) {
  const switched: Array<string | null> = [];
  const store = create<ChatState>((set, get) => ({
    ...createBranchesSlice(set, get),
    sessions,
    activeSessionId,
    forks: [],
    setActiveSession: (sid: string | null) => {
      switched.push(sid);
      set({ activeSessionId: sid });
    },
  } as unknown as ChatState));
  return { store, switched };
}

/** Wait for the background PATCH's .then/.catch to settle. */
const settled = () => new Promise((r) => setTimeout(r, 0));

function lastPatch(): [string, Record<string, unknown>] {
  const calls = vi.mocked(apiClient.patch).mock.calls;
  return calls[calls.length - 1] as [string, Record<string, unknown>];
}

describe('openBranch — thread root resolution', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiClient.patch).mockReset().mockResolvedValue({});
  });

  it('patches the ROOT from the active session\'s thread when the target sibling is unlisted', async () => {
    // The thread's list row previews branch d1; the switcher offers sib2,
    // which has NO row in `sessions`.
    const { store, switched } = buildStore([sess('d1', 'root1')], 'd1');

    store.getState().openBranch('sib2');
    await settled();

    expect(lastPatch()).toEqual(['/chat/sessions/root1', { active_branch_id: 'sib2' }]);
    expect(switched).toEqual(['sib2']);
    expect(store.getState().activeSessionId).toBe('sib2');
  });

  it('still patches the ROOT when switching between two unlisted siblings (remembered fact)', async () => {
    const { store } = buildStore([sess('d2', 'r2')], 'd2');

    // First hop: d2 → x2 (x2 unlisted; root learned from d2's row).
    store.getState().openBranch('x2');
    await settled();
    expect(lastPatch()).toEqual(['/chat/sessions/r2', { active_branch_id: 'x2' }]);

    // Second hop: x2 → y2 — NEITHER has a row now (x2 was never seated); the
    // remembered branch→root fact for the ACTIVE session carries the thread.
    store.getState().openBranch('y2');
    await settled();
    expect(lastPatch()).toEqual(['/chat/sessions/r2', { active_branch_id: 'y2' }]);
  });

  it('resolves the root in the fork-flow shape (branch row seated, source still active)', async () => {
    // forkAndResend/rewindTo prepend the branch row and call openBranch
    // BEFORE the switch: the target has a row AND the active source does —
    // both name the same root.
    const { store } = buildStore([sess('fresh', 'r3'), sess('src', 'r3')], 'src');

    store.getState().openBranch('fresh');
    await settled();

    expect(lastPatch()).toEqual(['/chat/sessions/r3', { active_branch_id: 'fresh' }]);
  });

  it('treats a pre-threading row as its own root (thread_id null)', async () => {
    const { store } = buildStore([sess('legacy', null)], 'legacy');

    store.getState().openBranch('legacy2');
    await settled();

    expect(lastPatch()).toEqual(['/chat/sessions/legacy2', { active_branch_id: 'legacy2' }]);
  });

  it('updates the root\'s sessions row from the PATCH response when the root is listed', async () => {
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 'root9', active_branch_id: 'br9' });
    const { store } = buildStore([sess('root9', 'root9')], 'root9');

    store.getState().openBranch('br9');
    await settled();

    expect(store.getState().sessions[0].active_branch_id).toBe('br9');
  });

  it('switches locally and toasts when the PATCH fails (no silent divergence)', async () => {
    vi.mocked(apiClient.patch).mockRejectedValue(new Error('network'));
    const { store, switched } = buildStore([sess('d4', 'root4')], 'd4');

    store.getState().openBranch('sib4');
    await settled();

    expect(switched).toEqual(['sib4']);
    expect(store.getState().activeSessionId).toBe('sib4');
    expect(showToast).toHaveBeenCalledWith('chatBranchSwitchFailed', 'warning');
  });
});
