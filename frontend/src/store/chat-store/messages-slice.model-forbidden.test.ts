/** chat-store send path — a turn refused with 403 `model_forbidden` tells the
 * user which model, refetches the roster and opens the composer's picker. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

vi.mock('../../chat/context', () => ({
  resolveCompletionContext: () => ({ context_document_ids: [], context_reference_ids: [] }),
}));
vi.mock('./streaming', () => ({
  streamCompletion: vi.fn(),
  flushStreaming: () => ({}),
  emptyStreaming: () => ({ messageId: null, content: '', controller: null }),
  adoptOpenTurn: vi.fn(),
  hasOpenHarnessTurn: vi.fn(() => false),
  markHarnessTurnAborted: vi.fn(),
}));
const showToast = vi.fn();
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), {
    getState: () => ({
      currentUser: { user_id: 'u1', name: 'U' },
      showToast,
      currentReference: null,
      currentDocument: null,
    }),
  }),
}));
vi.mock('../../i18n', () => ({
  t: (k: string, vars?: Record<string, string>) => (vars ? `${k}:${JSON.stringify(vars)}` : k),
}));

import { createMessagesSlice } from './messages-slice';
import { streamCompletion } from './streaming';
import { ForbiddenError } from '../../api/client';
import type { ChatState } from './types';

function buildStore() {
  const loadModels = vi.fn().mockResolvedValue(undefined);
  const store = create<ChatState>((set, get) => ({
    ...createMessagesSlice(set, get),
    sessions: [{
      session_id: 's1', document_id: null, reference_id: null, user_id: 'u1',
      title: 't', model: 'vendor/revoked', system_prompt_id: null, context_ids: [],
      updated_at: '2026-01-01T00:00:00Z', created_at: '2026-01-01T00:00:00Z', last_message_at: null,
    }],
    activeSessionId: 's1',
    messages: [],
    selectedSiblings: {},
    streaming: null,
    modelsLoaded: true,
    modelPickerOpen: false,
    loadModels,
    // runCompletion's restore-on-fail writes the composer (misc-slice in the
    // real store).
    draft: '',
    setDraft: (v: string) => set({ draft: v }),
  } as unknown as ChatState));
  return { store, loadModels };
}

describe('sendMessage — 403 model_forbidden', () => {
  beforeEach(() => vi.clearAllMocks());

  it('toasts the refused model, refetches the roster and opens the picker', async () => {
    vi.mocked(streamCompletion).mockRejectedValue(
      new ForbiddenError({ code: 'model_forbidden', model: 'vendor/revoked' }),
    );
    const { store, loadModels } = buildStore();
    await store.getState().sendMessage('hi');
    expect(showToast).toHaveBeenCalledWith(
      'chatModelForbidden:{"model":"vendor/revoked"}', 'error',
    );
    expect(store.getState().modelPickerOpen).toBe(true);
    expect(store.getState().modelsLoaded).toBe(false);
    expect(loadModels).toHaveBeenCalledTimes(1);
    // The session keeps the model the user chose — nothing swaps it.
    expect(store.getState().sessions[0].model).toBe('vendor/revoked');
  });

  it('a 403 of another kind stays the generic send failure, picker closed', async () => {
    vi.mocked(streamCompletion).mockRejectedValue(new ForbiddenError());
    const { store, loadModels } = buildStore();
    await store.getState().sendMessage('hi');
    expect(showToast).toHaveBeenCalledWith('chatSendFailed', 'error');
    expect(store.getState().modelPickerOpen).toBe(false);
    expect(loadModels).not.toHaveBeenCalled();
  });
});
