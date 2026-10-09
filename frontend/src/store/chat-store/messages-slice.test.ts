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
  // detail-carrying like the real client: the tail-conflict detection keys on
  // the status + detail pair.
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
import { createQueueSlice } from './queue-slice';
import { streamCompletion } from './streaming';
import { apiClient, HttpError } from '../../api/client';
import type { ChatMessage } from '../../types';
import type { ChatState } from './types';

function buildStore(sessions: ChatState['sessions'], activeSessionId: string | null) {
  return create<ChatState>((set, get) => ({
    ...createMessagesSlice(set, get),
    sessions,
    activeSessionId,
    messages: [],
   
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

describe('rewindTo — fork a branch ending before the message and open it', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  /** A stored message row (role/content defaulted for brevity). */
  function msg(
    id: string,
    parent_id: string | null,
    created_at: string,
    role: 'user' | 'assistant' = 'user',
  ): ChatMessage {
    return { message_id: id, chat_id: 'active', parent_id, role, content: id, created_at } as ChatMessage;
  }

  /** A store with the branch-flow stubs (openBranch / loadMessages / forks). */
  function branchStore(messages: ChatMessage[]) {
    const opened: string[] = [];
    const loads: string[] = [];
    const store = create<ChatState>((set, get) => ({
      ...createMessagesSlice(set, get),
      ...createQueueSlice(set, get),
      sessions: [sess('active', null, '2026-01-01T00:00:00Z')],
      activeSessionId: 'active',
      messages,
     
      forks: [],
      streaming: null,
      draft: '',
      queued: {},
      setDraft: (v: string) => set({ draft: v }),
      openBranch: (sid: string) => { opened.push(sid); set({ activeSessionId: sid }); },
      loadMessages: async (sid: string) => { loads.push(sid); },
    } as unknown as ChatState));
    return { store, opened, loads };
  }

  /** a(user, root) → b(user) → bReply(assistant). */
  const A = msg('a', null, '2026-01-01T00:00:00Z');
  const B = msg('b', 'a', '2026-01-01T00:01:00Z');
  const B_REPLY = msg('bReply', 'b', '2026-01-01T00:02:00Z', 'assistant');

  it('POSTs /branches after the message\'s parent, opens the branch, focuses the composer', async () => {
    const { store, opened } = branchStore([A, B, B_REPLY]);
    (apiClient.post as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ session_id: 'branch-1', thread_id: 'active' });

    await store.getState().rewindTo('b');

    expect(apiClient.post).toHaveBeenCalledWith(
      '/chat/sessions/active/branches', { after_message_id: 'a' });
    expect(opened).toEqual(['branch-1']);
    expect(store.getState().pendingInputFocus).toBe(true);
    // The thread stays ONE list entry: the branch replaces the row that
    // stood for its thread (the server previews the last-opened branch).
    expect(store.getState().sessions.map(s => s.session_id)).toEqual(['branch-1']);
  });

  it('never rewinds the first message — it is immutable (no root fork)', async () => {
    const { store } = branchStore([A]);
    await store.getState().rewindTo('a');
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(store.getState().pendingInputFocus).toBeUndefined();
  });

  it('refuses while streaming (no branch mid-turn)', async () => {
    const { store } = branchStore([A, B, B_REPLY]);
    store.setState({
      streaming: { sessionId: 'active', messageId: 'live', content: '', controller: null },
    } as unknown as Partial<ChatState>);
    await store.getState().rewindTo('b');
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('surfaces a failed branch create with an explicit toast and changes nothing', async () => {
    const { store, opened } = branchStore([A, B, B_REPLY]);
    (apiClient.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('network'));
    const toast = vi.fn();
    appStoreState.showToast = toast;

    await store.getState().rewindTo('b');

    expect(toast).toHaveBeenCalledWith('chatBranchCreateFailed', 'error');
    expect(opened).toEqual([]);
    expect(store.getState().activeSessionId).toBe('active');
  });
});

describe('sendMessage — the non-tail 409 contract: one re-read + one retry', () => {
  /** The backend's stale-tail refusal (plan: the linear-turn contract). */
  const conflictDetail = '{"detail":"parent_id is not the tail of this branch — re-read the branch and retry"}';

  beforeEach(() => {
    vi.clearAllMocks();
    // Drop any once-implementations a failed prior test left queued — they
    // would leak into this describe's first call.
    vi.mocked(streamCompletion).mockReset().mockResolvedValue(undefined);
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  /** A store whose loadMessages seats `fresh` rows (the re-read's product). */
  function conflictStore(fresh: ChatMessage[]) {
    const loads: string[] = [];
    const store = create<ChatState>((set, get) => ({
      ...createMessagesSlice(set, get),
      ...createQueueSlice(set, get),
      sessions: [sess('active', null, '2026-01-01T00:00:00Z')],
      activeSessionId: 'active',
      messages: [],
     
      forks: [],
      streaming: null,
      draft: '',
      queued: {},
      setDraft: (v: string) => set({ draft: v }),
      loadMessages: async (sid: string) => {
        loads.push(sid);
        set({ messages: fresh });
      },
    } as unknown as ChatState));
    return { store, loads };
  }

  it('retries ONCE after re-reading; the retry parents on the fresh tail', async () => {
    // A connected fresh chain (the re-read's product): fresh-user → fresh-tail.
    const fresh = [
      { message_id: 'fresh-user', chat_id: 'active', parent_id: null, role: 'user', content: 're-sent', created_at: '2026-01-02T00:00:00Z' },
      { message_id: 'fresh-tail', chat_id: 'active', parent_id: 'fresh-user', role: 'assistant', content: 'x', created_at: '2026-01-02T00:01:00Z' },
    ] as ChatMessage[];
    const { store, loads } = conflictStore(fresh);
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(streamCompletion)
      .mockRejectedValueOnce(new HttpError(409, conflictDetail))
      .mockResolvedValueOnce(undefined);

    await store.getState().sendMessage('hi');

    // One conflict → one re-read → one retry, and no failure surfaced.
    expect(streamCompletion).toHaveBeenCalledTimes(2);
    expect(loads).toEqual(['active']);
    expect(toast).not.toHaveBeenCalled();
    // The retried turn appended onto the RE-READ branch's tail.
    expect(lastBody().parent_id).toBe('fresh-tail');
    // The failed attempt's optimistic bubble is gone; the retry's is the
    // only temp row left.
    expect(store.getState().messages.filter(m => m.message_id.startsWith('temp-'))).toHaveLength(1);
  });

  it('a SECOND conflict surfaces the explicit toast — never a third attempt', async () => {
    const { store } = conflictStore([]);
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(streamCompletion)
      .mockRejectedValueOnce(new HttpError(409, conflictDetail))
      .mockRejectedValueOnce(new HttpError(409, conflictDetail));

    await store.getState().sendMessage('hi');

    expect(streamCompletion).toHaveBeenCalledTimes(2);
    expect(toast).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith('chatBranchMovedOn', 'error');
    // No phantom bubble after the final failure.
    expect(store.getState().messages).toEqual([]);
  });

  it('the turn-lock 409 (a turn is in progress) is NOT retried', async () => {
    const { store } = conflictStore([]);
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(streamCompletion).mockRejectedValueOnce(
      new HttpError(409, '{"detail":"A turn is already in progress on this chat."}'));

    await store.getState().sendMessage('hi');

    expect(streamCompletion).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith('chatSendFailed', 'error');
  });
});

describe('sendMessage — mid-turn send routes to the message queue', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appStoreState.currentReference = null;
    appStoreState.currentDocument = null;
  });

  /** A store with a mid-turn streaming slot (an open turn owns it). */
  function busyStore() {
    return create<ChatState>((set, get) => ({
      ...createMessagesSlice(set, get),
      ...createQueueSlice(set, get),
      sessions: [sess('active', null, '2026-01-01T00:00:00Z')],
      activeSessionId: 'active',
      messages: [],
     
      queued: {},
      streaming: { sessionId: 'active', messageId: 'm1', content: '', controller: null },
    } as unknown as ChatState));
  }

  it('a mid-turn send starts no turn — the text joins the session queue', async () => {
    const store = busyStore();
    await store.getState().sendMessage('follow-up');
    expect(streamCompletion).not.toHaveBeenCalled();
    expect(store.getState().queued['active']).toEqual(['follow-up']);
  });

  it('the open turn keeps its streaming slot', async () => {
    const store = busyStore();
    await store.getState().sendMessage('follow-up');
    expect(store.getState().streaming?.messageId).toBe('m1');
  });
});
