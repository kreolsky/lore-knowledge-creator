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
import { createQueueSlice } from './queue-slice';
import { streamCompletion } from './streaming';
import { REWIND_KEY, ROOT_KEY, resolveActivePath, isPathRewound } from './tree';
import { apiClient } from '../../api/client';
import type { ChatMessage } from '../../types';
import type { ChatState } from './types';

function buildStore(sessions: ChatState['sessions'], activeSessionId: string | null) {
  return create<ChatState>((set, get) => ({
    ...createMessagesSlice(set, get),
    sessions,
    activeSessionId,
    messages: [],
    selectedSiblings: {},
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

describe('rewindTo / cancelRewind — REWIND_KEY sentinel', () => {
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

  /** Default fixture: a(null) → b(user) → bReply; c is b's fresher sibling. */
  function rewindStore() {
    const store = buildStore([sess('active', null, '2026-01-01T00:00:00Z')], 'active');
    store.setState({
      messages: [
        msg('a', null, '2026-01-01T00:00:00Z'),
        msg('b', 'a', '2026-01-01T00:01:00Z'),
        msg('bReply', 'b', '2026-01-01T00:02:00Z', 'assistant'),
        msg('c', 'a', '2026-01-01T00:03:00Z'),
      ],
    } as unknown as Partial<ChatState>);
    return store;
  }

  it('resolveActivePath with a REWIND_KEY selection ends the path at that level, excluding every child', () => {
    const { messages } = rewindStore().getState();
    // Sentinel keyed at b's parent (a): the path renders up to and including a.
    const path = resolveActivePath(messages, { a: REWIND_KEY });
    expect(path.map(m => m.message_id)).toEqual(['a']);
  });

  it('resolveActivePath with the sentinel at ROOT_KEY returns an empty path', () => {
    const { messages } = rewindStore().getState();
    expect(resolveActivePath(messages, { [ROOT_KEY]: REWIND_KEY })).toEqual([]);
  });

  it('rewindTo(m) then sendMessage parents the new turn on m.parent_id and excludes the hidden branch', async () => {
    const store = rewindStore();
    store.getState().rewindTo('b');
    await store.getState().sendMessage('x');
    const body = lastBody();
    expect(body.parent_id).toBe('a');
    const contents = (body.messages as Array<{ role: string; content: string }>).map(m => m.content);
    expect(contents).toEqual(['a', 'x']);
    expect(contents).not.toContain('b');
    expect(contents).not.toContain('bReply');
  });

  it('after the post-rewind send the hidden branch survives as a sibling (fork switcher offers both)', async () => {
    const store = rewindStore();
    store.getState().rewindTo('b');
    await store.getState().sendMessage('x');
    const sib = store.getState().getSiblings('a').map(m => m.message_id);
    expect(sib).toContain('b');
    expect(sib).toContain('c');
    // The optimistic user message is the third sibling at the same level.
    expect(sib).toHaveLength(3);
  });

  it('rewindTo a first message → sendMessage posts parent_id: null (root sibling)', async () => {
    const store = rewindStore();
    store.setState({ messages: [msg('a', null, '2026-01-01T00:00:00Z')] } as unknown as Partial<ChatState>);
    store.getState().rewindTo('a');
    await store.getState().sendMessage('x');
    expect(lastBody().parent_id).toBeNull();
    expect(lastBody().messages).toEqual([{ role: 'user', content: 'x', images: undefined }]);
  });

  it('a second rewindTo moves the single sentinel (exactly one REWIND_KEY entry)', () => {
    const store = rewindStore();
    store.getState().rewindTo('b');      // sentinel at 'a'
    store.getState().rewindTo('bReply'); // sentinel moves to bReply's parent ('b')
    const s = store.getState().selectedSiblings;
    const sentinelKeys = Object.entries(s).filter(([, v]) => v === REWIND_KEY).map(([k]) => k);
    expect(sentinelKeys).toEqual(['b']);
  });

  it('rewindTo while streaming changes nothing', () => {
    const store = rewindStore();
    store.setState({ streaming: { messageId: 'live', content: '', controller: null } } as unknown as Partial<ChatState>);
    store.getState().rewindTo('b');
    expect(store.getState().selectedSiblings).toEqual({});
  });

  it('cancelRewind restores the pre-rewind path deep-equal', () => {
    const store = rewindStore();
    const { messages } = store.getState();
    const before = resolveActivePath(messages, {});
    store.getState().rewindTo('b');
    expect(resolveActivePath(messages, store.getState().selectedSiblings).map(m => m.message_id)).toEqual(['a']);
    store.getState().cancelRewind();
    expect(resolveActivePath(messages, store.getState().selectedSiblings)).toEqual(before);
  });

  it('selectSibling above the cut drops the sentinel — switching back shows the full old branch', () => {
    const store = rewindStore();
    store.getState().rewindTo('bReply'); // sentinel at 'b' (inside b's branch)
    store.getState().selectSibling('a', 'c'); // user switches to the c branch
    store.getState().selectSibling('a', 'b'); // ...and back to b
    const s = store.getState();
    expect(Object.values(s.selectedSiblings).includes(REWIND_KEY)).toBe(false);
    expect(resolveActivePath(s.messages, s.selectedSiblings).map(m => m.message_id)).toEqual(['a', 'b', 'bReply']);
  });

  it('isPathRewound is true only while the rendered path ends at the cut', () => {
    const { messages } = rewindStore().getState();
    const cut = { a: REWIND_KEY };
    expect(isPathRewound(resolveActivePath(messages, cut), cut)).toBe(true);
    // Sentinel parked inside a branch the path does not walk: inert, no plaque.
    const offPath = { a: 'c', b: REWIND_KEY };
    expect(isPathRewound(resolveActivePath(messages, offPath), offPath)).toBe(false);
    const root = { [ROOT_KEY]: REWIND_KEY };
    expect(isPathRewound(resolveActivePath(messages, root), root)).toBe(true);
  });

  it('a failed send from a rewound state keeps the rewind (sentinel back at the parent, branch still hidden)', async () => {
    vi.mocked(streamCompletion).mockRejectedValueOnce(new Error('network'));
    const store = rewindStore();
    store.getState().rewindTo('b');
    await store.getState().sendMessage('x');
    const s = store.getState();
    expect(s.selectedSiblings['a']).toBe(REWIND_KEY);
    // Hidden branch still hidden: the path excludes b and everything below it.
    expect(resolveActivePath(s.messages, s.selectedSiblings).map(m => m.message_id)).toEqual(['a']);
    // The phantom optimistic bubble is gone (no-silent-degradation unchanged).
    expect(s.messages.some(m => m.content === 'x')).toBe(false);
  });

  it('a failed send from a NON-rewound state plants no sentinel', async () => {
    vi.mocked(streamCompletion).mockRejectedValueOnce(new Error('network'));
    const store = rewindStore();
    await store.getState().sendMessage('x');
    const s = store.getState();
    expect(Object.values(s.selectedSiblings).includes(REWIND_KEY)).toBe(false);
    // The optimistic bubble is still rolled back — the restore flag must not
    // turn a plain failure into a rewind.
    expect(resolveActivePath(s.messages, s.selectedSiblings).map(m => m.message_id)).toEqual(['a', 'c']);
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
      selectedSiblings: {},
      queued: {},
      streaming: { messageId: 'm1', content: '', controller: null },
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
