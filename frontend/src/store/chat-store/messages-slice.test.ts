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
import { streamCompletion, rememberQueuedSend, forgetQueuedSend } from './streaming';
import { apiClient, HttpError } from '../../api/client';
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
      sessions: [sess('active', null, '2026-01-01T00:00:00Z')],
      activeSessionId: 'active',
      messages,
      forks: [],
      streaming: null,
      draft: '',
      inboxQueue: [],
      dshRunning: false,
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

  it('idle: the optimistic bubble carries the user_msg_id the POST sends', async () => {
    const st = store();
    await st.getState().sendMessage('hi');
    const id = lastBody().user_msg_id as string;
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
    expect(st.getState().messages.map(m => m.message_id)).toEqual([id]);
    expect(completionPosts()).toHaveLength(0);
  });

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
    expect(rememberQueuedSend).toHaveBeenCalledWith(body.user_msg_id, 'follow-up', ['data:image/png;base64,AAA']);
  });

  it('busy: an open turn keeps its streaming slot', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({ accepted: true });
    const st = store({ streaming: { sessionId: 'active', messageId: 'm1', content: '', controller: null } } as Partial<ChatState>);
    await st.getState().sendMessage('follow-up');
    expect(st.getState().streaming?.messageId).toBe('m1');
  });

  it('busy, refused (422 with a reason): toasts the reason, the text returns to the composer', async () => {
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(apiClient.post).mockRejectedValueOnce(
      new HttpError(422, '{"detail":"No model is configured for this chat."}'));
    const st = store({ dshRunning: true } as Partial<ChatState>);
    await st.getState().sendMessage('follow-up');
    expect(toast).toHaveBeenCalledWith('No model is configured for this chat.', 'error');
    expect(forgetQueuedSend).toHaveBeenCalled();
    expect(st.getState().draft).toBe('follow-up');
  });

  it('idle, refused (422 with a reason): the bubble is dropped and the reason toasted', async () => {
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(streamCompletion).mockRejectedValueOnce(
      new HttpError(422, '{"detail":"No model is configured for this chat."}'));
    const st = store();
    await st.getState().sendMessage('hi');
    expect(toast).toHaveBeenCalledWith('No model is configured for this chat.', 'error');
    expect(st.getState().messages).toEqual([]);
    expect(st.getState().draft).toBe('hi');
  });

  it('a request-validation 422 (a list detail) falls back to the generic toast', async () => {
    const toast = vi.fn();
    appStoreState.showToast = toast;
    vi.mocked(streamCompletion).mockRejectedValueOnce(
      new HttpError(422, '{"detail":[{"loc":["body","user_msg_id"],"msg":"bad"}]}'));
    await store().getState().sendMessage('hi');
    expect(toast).toHaveBeenCalledWith('chatSendFailed', 'error');
  });
});

describe('stopGeneration — a dsh-running chat with no slot of this tab', () => {
  it('still POSTs the cancel', () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({});
    const st = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    st.setState({ dshRunning: true } as Partial<ChatState>);
    st.getState().stopGeneration();
    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions/active/completions/cancel', {});
  });

  it("returns the queue Stop took out to the composer — text after any draft, images as attachments", async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce({
      cancelled: true,
      returned: [{ text: 'B', images: ['data:image/png;base64,AAA'] }, { text: 'C', images: [] }],
    });
    const st = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    const added: string[] = [];
    st.setState({
      dshRunning: true, draft: 'typed',
      addPendingImage: (url: string) => { added.push(url); },
    } as unknown as Partial<ChatState>);
    st.getState().stopGeneration();
    await new Promise((r) => setTimeout(r, 0));
    expect(st.getState().draft).toBe('typed\n\nB\n\nC');
    expect(added).toEqual(['data:image/png;base64,AAA']);
  });

  it('does nothing when the chat is idle', () => {
    vi.mocked(apiClient.post).mockClear();
    const st = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    st.getState().stopGeneration();
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});
