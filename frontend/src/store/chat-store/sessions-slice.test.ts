/** sessions-slice updateSession — preserves last_message_at on settings PATCH(review fix). */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

// Mock only what updateSession's success path touches: apiClient.patch + the
// app-store toast (error path). The pending-patch helpers (./inflight) run real.
const patchMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: { patch: (...args: unknown[]) => patchMock(...args) },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), { getState: () => ({ showToast: vi.fn() }) }),
}));

import { createSessionsSlice } from './sessions-slice';
import type { ChatState } from './types';

function sess(id: string, lm: string | null, updated: string): ChatState['sessions'][number] {
  return {
    session_id: id,
    document_id: null,
    reference_id: null,
    user_id: 'u1',
    title: `t-${id}`,
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    updated_at: updated,
    last_message_at: lm,
  } as unknown as ChatState['sessions'][number];
}

function buildStore(sessions: ChatState['sessions']) {
  return create<ChatState>((set, get) => ({
    ...createSessionsSlice(set, get),
    sessions,
    activeSessionId: 's1',
  } as unknown as ChatState));
}

describe('updateSession — last_message_at preservation', () => {
  beforeEach(() => vi.clearAllMocks());

  it('preserves the in-memory last_message_at when the PATCH response nulls it', async () => {
    // Backend update_session returns last_message_at=null (no last-activity map).
    patchMock.mockResolvedValue({
      session_id: 's1',
      title: 't-s1',
      model: 'newmodel',
      system_prompt_id: null,
      context_ids: [],
      updated_at: '2026-07-06T12:00:00Z',
      last_message_at: null,
    } as unknown);
    const store = buildStore([sess('s1', '2026-03-15T12:00:00Z', '2026-01-01T00:00:00Z')]);
    await store.getState().updateSession('s1', { model: 'newmodel' });
    const s1 = store.getState().sessions.find(s => s.session_id === 's1')!;
    expect(s1.model).toBe('newmodel');           // PATCH applied
    expect(s1.last_message_at).toBe('2026-03-15T12:00:00Z');  // in-memory value preserved
  });

  it('uses the response last_message_at when the PATCH provides one', async () => {
    patchMock.mockResolvedValue({
      session_id: 's1',
      title: 't-s1',
      model: 'm',
      system_prompt_id: null,
      context_ids: [],
      updated_at: '2026-07-06T12:00:00Z',
      last_message_at: '2026-06-01T00:00:00Z',
    } as unknown);
    const store = buildStore([sess('s1', '2026-03-15T12:00:00Z', '2026-01-01T00:00:00Z')]);
    await store.getState().updateSession('s1', { model: 'm' });
    expect(store.getState().sessions[0].last_message_at).toBe('2026-06-01T00:00:00Z');
  });
});
