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
  flushStreaming: () => ({}),
  emptyStreaming: () => ({
    messageId: null, content: '', controller: null,
  }),
  adoptOpenTurn: vi.fn(),
  hasOpenHarnessTurn: vi.fn(() => false),
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
  RequestTooLargeError: class extends Error {},
}));

import { createMessagesSlice } from './messages-slice';
import { streamCompletion } from './streaming';
import { apiClient } from '../../api/client';
import type { ChatState } from './types';

function buildStore(sessions: ChatState['sessions'], activeSessionId: string | null) {
  return create<ChatState>((set, get) => ({
    ...createMessagesSlice(set, get),
    sessions,
    activeSessionId,
    messages: [],
    selectedSiblings: {},
    streaming: null,
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

function lastBody(): Record<string, unknown> {
  const calls = vi.mocked(streamCompletion).mock.calls;
  return (calls[calls.length - 1][2] as { body: Record<string, unknown> }).body;
}

describe('sendMessage — open_doc_id (live open document)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  it('sends currentReference.reference_id when a reference is open (ghost-base priority)', async () => {
    appStoreState.currentReference = { reference_id: 'ref-9' };
    appStoreState.currentDocument = { document_id: 'doc-1' };
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    await store.getState().sendMessage('hi');
    expect(lastBody().open_doc_id).toBe('ref-9');
  });

  it('falls back to currentDocument.document_id when no reference is open', async () => {
    appStoreState.currentDocument = { document_id: 'doc-1' };
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    await store.getState().sendMessage('hi');
    expect(lastBody().open_doc_id).toBe('doc-1');
  });

  it('omits open_doc_id entirely when nothing is open', async () => {
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    await store.getState().sendMessage('hi');
    expect('open_doc_id' in lastBody()).toBe(false);
  });
});

describe('sendMessage — last_message_at bump', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  it('bumps the active session last_message_at so the card date + list position refresh', async () => {
    const store = buildStore(
      [sess('older', null, '2026-06-01T00:00:00Z'), sess('active', null, '2026-01-01T00:00:00Z')],
      'active',
    );
    await store.getState().sendMessage('hello');
    const sessions = store.getState().sessions;
    const active = sessions.find(s => s.session_id === 'active')!;
    const older = sessions.find(s => s.session_id === 'older')!;
    expect(active.last_message_at).toBeTruthy();
    // Other sessions untouched.
    expect(older.last_message_at).toBeNull();
    // The bumped active session now sorts FIRST (newer than older.created_at).
    // Recency fallback is created_at, NOT
    // updated_at (which update_session bumps and would reorder on
    // enter→leave). Both fixtures carry created_at so the assertion exercises it.
    const sorted = [...sessions].sort((a, b) =>
      (b.last_message_at ?? b.created_at).localeCompare(a.last_message_at ?? a.created_at),
    );
    expect(sorted[0].session_id).toBe('active');
  });

  it('inserts an optimistic user message in addition to the session bump', async () => {
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    await store.getState().sendMessage('hi');
    expect(store.getState().messages).toHaveLength(1);
    expect(store.getState().messages[0].role).toBe('user');
    expect(store.getState().messages[0].content).toBe('hi');
  });
});

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
      streaming: { messageId: 'm1', content: '', controller },
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

describe('sendMessage — mid-turn send (plan agent-line-harness-lifecycle step 9: the queue died)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  /** A store with a mid-turn streaming slot (an open turn owns it). */
  function busyStore() {
    return create<ChatState>((set, get) => ({
      ...createMessagesSlice(set, get),
      sessions: [sess('active', null, '2026-01-01T00:00:00Z')],
      activeSessionId: 'active',
      messages: [],
      selectedSiblings: {},
      streaming: { messageId: 'm1', content: '', controller: null },
    } as unknown as ChatState));
  }

  it('a mid-turn send POSTs unconditionally — the backend lock is the serializer', async () => {
    const store = busyStore();
    await store.getState().sendMessage('follow-up');
    expect(streamCompletion).toHaveBeenCalledTimes(1);
  });

  it('the bystander send owns no streaming slot — the open turn keeps its messageId', async () => {
    const store = busyStore();
    await store.getState().sendMessage('follow-up');
    // The foreign turn's slot was neither replaced nor cleared by the bystander.
    expect(store.getState().streaming?.messageId).toBe('m1');
  });
});
