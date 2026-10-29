/** chat-store sendMessage — bumps active session last_message_at on send. */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

// Mock the send-path dependencies so sendMessage runs without a live frame stream
// or DOM editor state. The optimistic bump happens synchronously inside
// insertOptimisticUser BEFORE runCompletion awaits, so a no-op stream is enough.
vi.mock('../../chat/context', () => ({
  resolveCompletionContext: () => ({ context_document_ids: [], context_reference_ids: [] }),
}));
vi.mock('./streaming', () => ({
  streamCompletion: vi.fn().mockResolvedValue(undefined),
  // The REAL reducer returns { streaming: null } — the conflict-retry path
  // re-reads the slot after this flush, so a no-op return would wedge it.
  flushStreaming: () => ({ streaming: null }),
  emptyStreaming: () => ({
    messageId: null, content: '', controller: null,
  }),
  adoptOpenTurn: vi.fn(),
  hasOpenHarnessTurn: vi.fn(() => false),
  markHarnessTurnAborted: vi.fn(),
  rememberQueuedSend: vi.fn(),
  forgetQueuedSend: vi.fn(),
}));
// Mutable app-store state so individual tests can set the open document
// (currentReference / currentDocument) that runCompletion reads at send time.
const appStoreState: Record<string, unknown> = {
  currentUser: { user_id: 'u1', name: 'Serge' },
  showToast: vi.fn(),
  currentReference: null,
  currentDocument: null,
};
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(
    () => ({}),
    { getState: () => appStoreState },
  ),
}));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
vi.mock('../../api/client', () => ({
  apiClient: { post: vi.fn() },
  // detail-carrying like the real client: the refusal toast reads the
  // reason out of the detail body.
  HttpError: class extends Error {
    status: number;
    detail: string;
    constructor(status: number, detail = '') {
      super(`HTTP ${status}${detail ? `: ${detail}` : ''}`);
      this.name = 'HttpError';
      this.status = status;
      this.detail = detail;
    }
  },
  RequestTooLargeError: class extends Error {},
}));

import { createMessagesSlice } from './messages-slice';
import { streamCompletion, rememberQueuedSend } from './streaming';
import { apiClient } from '../../api/client';
import type { ChatMessage } from '../../types';
import type { ChatState } from './types';

function buildStore(sessions: ChatState['sessions'], activeSessionId: string | null) {
  return create<ChatState>((set, get) => ({
    ...createMessagesSlice(set, get),
    sessions,
    activeSessionId,
    messages: [],
    inboxQueue: [],
    dshRunning: false,
    liveSend: null,
    streaming: null,
    // runCompletion's restore-on-fail writes the composer (misc-slice in the
    // real store).
    draft: '',
    setDraft: (v: string) => set({ draft: v }),
  } as unknown as ChatState));
}

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
    // created_at mirrors the passed value so sort-fallback assertions have data.
    created_at: updated,
    last_message_at: lm,
  } as unknown as ChatState['sessions'][number];
}

describe('stopGeneration — cancel POST feedback', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('toasts stopGenerationFailed when the cancel POST rejects (client abort still fires)', async () => {
    // T5 (tech-debt audit): the cancel POST was .catch(() => {}) — a failed cancel
    // left a zombie server-side turn with zero feedback. Now it toasts.
    vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('network'));

    const controller = { abort: vi.fn() } as unknown as AbortController;
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    store.setState({
      streaming: { sessionId: 'active', messageId: 'm1', content: '', controller },
    } as unknown as Partial<ChatState>);
    const toast = vi.fn();
    appStoreState.showToast = toast;

    store.getState().stopGeneration();
    await new Promise((r) => setTimeout(r, 0));

    // Client abort fires regardless (local stop is best-effort + immediate).
    expect(controller.abort).toHaveBeenCalled();
    expect(apiClient.post).toHaveBeenCalledWith(
      '/chat/sessions/active/completions/cancel', {},
    );
    expect(toast).toHaveBeenCalledWith('stopGenerationFailed', 'error');
  });
});

describe('sendMessage — an idle send opens its turn, a busy one queues in dsh', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(streamCompletion).mockReset().mockResolvedValue(undefined);
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  function store(patch: Partial<ChatState> = {}) {
    const st = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    st.setState(patch);
    return st;
  }

  const completionPosts = () => vi.mocked(apiClient.post).mock.calls
    .filter(c => c[0] === '/chat/sessions/active/completions');

  it.each([
    ['an open slot', { streaming: { sessionId: 'active', messageId: 'm1', content: '', controller: null } }],
    ['dsh running', { dshRunning: true }],
    ['messages already queued', { inboxQueue: [{ id: 'q1', text: 'earlier', images: 0 }] }],
  ])('busy (%s): POSTs the message with no optimistic row and no turn of its own', async (_label, patch) => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({ accepted: true, message_id: 'dm-2' });
    const st = store(patch as Partial<ChatState>);
    await st.getState().sendMessage('follow-up', ['data:image/png;base64,AAA']);
    expect(streamCompletion).not.toHaveBeenCalled();
    expect(st.getState().messages).toEqual([]);
    const body = completionPosts()[0][1] as Record<string, unknown>;
    expect(body.user_msg_id).toMatch(/^[0-9a-f-]{36}$/);
    // The user bubble's text and images wait for the claim's `ids`.
    expect(rememberQueuedSend).toHaveBeenCalledWith(body.user_msg_id, 'follow-up', ['data:image/png;base64,AAA'], undefined);
  });
});
