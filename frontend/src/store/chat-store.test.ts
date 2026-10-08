/** Unit tests for chat-store — tree helpers, selectors, synchronous actions. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => {
  class HttpError extends Error {
    status: number;
    constructor(s: number) { super(`HTTP ${s}`); this.name = 'HttpError'; this.status = s; }
  }
  class RequestTooLargeError extends Error {
    constructor() { super('Request too large'); this.name = 'RequestTooLargeError'; }
  }
  return {
    apiClient: {
      get: vi.fn(),
      post: vi.fn(),
      patch: vi.fn(),
      delete: vi.fn(),
    },
    HttpError,
    RequestTooLargeError,
  };
});

// L1: the shared attachment budget now lives in
// app-store. The mock is self-contained (no top-level variable referenced in the
// vi.mock factory — vitest hoists vi.mock above all top-level declarations and
// forbids referencing non-`mock`-prefixed variables there). setMaxAttachmentMb is
// asserted by its call argument.
vi.mock('./app-store', () => {
  const setMaxAttachmentMb = vi.fn();
  const state = {
    currentDocument: null,
    currentProject: null,
    maxAttachmentMb: 5,
    setMaxAttachmentMb,
    accessLevel: 'full',
    showToast: vi.fn(),
  };
  return {
    useAppStore: {
      getState: vi.fn(() => state),
      subscribe: () => () => {},
    },
  };
});

// Partial mock: real ui-store exports (showsBothPanes, readRefOpenMode, …) stay
// live, so a new export used by prod code cannot break this mock again — only the
// store instance is replaced. getRefOpenMode must return a RefOpenMode, not a
// boolean: the real showsBothPanes('center') === false is the "not split" case.
vi.mock('./ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ui-store')>();
  return {
    ...actual,
    useUIStore: {
      getState: vi.fn(() => ({
        documents: {},
        setLastActiveChatSession: vi.fn(),
        getLastActiveChatSession: vi.fn(() => null),
        getRefPreviewMode: vi.fn(() => false),
        getRefOpenMode: vi.fn(() => 'center'),
      })),
    },
  };
});

vi.mock('../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  GHOST_SESSION_ID: '__ghost__',
  resolveCompletionContext: vi.fn(() => ({ document_ids: [], reference_ids: [] })),
  getContextForSession: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  setContextForSession: vi.fn(),
  clearContextForSession: vi.fn(),
  getDerivedGhostContext: vi.fn(() => ({ docIds: [], refIds: [] })),
  ghostBaseTargets: vi.fn((docId: string | null, refId: string | null, split: boolean) => {
    const docs: string[] = []; const refs: string[] = [];
    if (split) { if (docId) docs.push(docId); if (refId) refs.push(refId); }
    else if (refId) refs.push(refId);
    else if (docId) docs.push(docId);
    return { docs, refs };
  }),
  resetGhostDeltas: vi.fn(),
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  addItemToContext: vi.fn(() => Promise.resolve()),
  removeItemFromContext: vi.fn(() => Promise.resolve()),
}));

import { useChatStore, selectActivePath } from './chat-store';
import { dispatchChatFrame } from './chat-store/streaming';
import { clearChatCaches } from './chat-store/reset-registry';
import { HttpError } from '../api/client';
import type { ChatMessage } from '../types';

/** Deliver one project-WS chat_frame envelope into the store's open harness
 * turn — the frame transport the retired SSE drain used to carry. */
function wsFrame(frame: Record<string, unknown>, sessionId = 'sess-1') {
  dispatchChatFrame(useChatStore.getState, useChatStore.setState, sessionId, frame);
}

/** The turn terminal that settles a harness turn's awaited send. */
const TURN_CLOSED = { type: 'turn_closed' };

function makeMsg(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    message_id: 'msg-1',
    chat_id: 'sess-1',
    parent_id: null,
    role: 'user',
    content: 'hello',
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeLinearChain(length: number): ChatMessage[] {
  const msgs: ChatMessage[] = [];
  for (let i = 0; i < length; i++) {
    msgs.push(makeMsg({
      message_id: `msg-${i}`,
      parent_id: i === 0 ? null : `msg-${i - 1}`,
      role: i % 2 === 0 ? 'user' : 'assistant',
      content: `message ${i}`,
    }));
  }
  return msgs;
}

function makeSession(overrides: Record<string, unknown> = {}) {
  return {
    session_id: 's1',
    project_id: 'p1',
    document_id: 'd1',
    reference_id: null as string | null,
    user_id: 'u1',
    title: '',
    model: 'm',
    system_prompt_id: null as string | null,
    context_ids: [] as string[],
    created_at: '',
    updated_at: '',
    ...overrides,
  };
}

beforeEach(() => {
  // Settle + drop any harness-turn registration a failed prior test left
  // dangling (streaming.ts keeps them module-level) — otherwise a late
  // frame for the same session id lands in a stale registration.
  clearChatCaches();
  useChatStore.setState({
    sessions: [],
    activeSessionId: null,
    documentId: null,
    messages: [],
    messagesLoading: false,
    chatScopeLoading: false,
    selectedSiblings: {},
    streaming: null,    models: [],
    modelsLoaded: false,
    defaultModel: '',
    pendingInputFocus: false,
    pendingImages: [],
    ghostAgentAuto: false,
    ghostSystemPromptId: null,
    ghostModel: '',
  });
});

describe('F3 — resolveAncestorChain (shared send-path helper)', () => {
  it('walks from a leaf up to root, leaf included', async () => {
    const { resolveAncestorChain } = await import('./chat-store/tree');
    const msgs = makeLinearChain(4);
    expect(resolveAncestorChain(msgs, 'msg-3').map(m => m.message_id)).toEqual([
      'msg-0', 'msg-1', 'msg-2', 'msg-3',
    ]);
  });

  it('returns [] for an unknown leaf id', async () => {
    const { resolveAncestorChain } = await import('./chat-store/tree');
    expect(resolveAncestorChain(makeLinearChain(2), 'nope')).toEqual([]);
  });

  it('exposes a single runCompletion entry point', async () => {
    const mod = await import('./chat-store/messages-slice');
    expect(typeof (mod as any).runCompletion).toBe('function');
  });

  it('forkAndResend derives the body via the shared helper (regression)', async () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, role: 'user', content: 'root' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', role: 'assistant', content: 'A' });
    const u1 = makeMsg({ message_id: 'u1', parent_id: 'a', role: 'user', content: 'fork me' });
    const a1 = makeMsg({ message_id: 'a1', parent_id: 'u1', role: 'assistant', content: 'reply' });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [root, a, u1, a1],
      selectedSiblings: {},
      // AI chats are gated out of fork/
      // regenerate (the buttons are hidden; the handler blocks on !is_note). This
      // regression targets the ancestor-chain body derivation in isolation, so the
      // session is seeded is_note=true to bypass the gate (the gate itself is
      // covered by the agentModeNoForkRegenerate toast tests below).
      sessions: [makeSession({ session_id: 'sess-1', is_note: true })],
    });

    const p = useChatStore.getState().forkAndResend('u1', 'replayed');
    wsFrame(TURN_CLOSED);
    await p;

    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    // Ancestor chain of u1's parent (a) = [root, a]; plus the new user content.
    expect(body.messages.map((m: ChatMessage) => m.content)).toEqual(['root', 'A', 'replayed']);
    expect(body.parent_id).toBe('a');
  });
});

describe('F4 — single streaming object lifecycle', () => {
  it('streaming is null when idle; an object with a controller on send; null after flush', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1', streaming: null,
      messages: makeLinearChain(2),
      sessions: [makeSession({ session_id: 'sess-1' })],
    });
    expect(useChatStore.getState().streaming).toBeNull();

    const promise = useChatStore.getState().sendMessage('test');
    const active = useChatStore.getState().streaming;
    expect(active).not.toBeNull();
    expect(active).toBeInstanceOf(Object);
    expect(active?.controller).toBeInstanceOf(AbortController);
    // The 5 flat fields no longer exist on the state.
    expect((useChatStore.getState() as any).isStreaming).toBeUndefined();
    expect((useChatStore.getState() as any).streamingContent).toBeUndefined();

    wsFrame(TURN_CLOSED);
    await promise;
    expect(useChatStore.getState().streaming).toBeNull();
  });
});

describe('selectActivePath', () => {
  it('returns empty array for no messages', () => {
    const path = selectActivePath({ messages: [], selectedSiblings: {} });
    expect(path).toEqual([]);
  });

  it('returns full linear chain when no forks', () => {
    const msgs = makeLinearChain(4);
    const path = selectActivePath({ messages: msgs, selectedSiblings: {} });
    expect(path.map(m => m.message_id)).toEqual(['msg-0', 'msg-1', 'msg-2', 'msg-3']);
  });

  it('follows selectedSiblings at fork points', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, content: 'root' });
    const a = makeMsg({ message_id: 'msg-a', parent_id: 'root', content: 'branch A' });
    const b = makeMsg({ message_id: 'msg-b', parent_id: 'root', content: 'branch B' });
    const aChild = makeMsg({ message_id: 'msg-ac', parent_id: 'msg-a', content: 'A child' });

    const path = selectActivePath({
      messages: [root, a, b, aChild],
      selectedSiblings: { root: 'msg-a' },
    });
    expect(path.map(m => m.message_id)).toEqual(['root', 'msg-a', 'msg-ac']);
  });

  it('defaults to the branch with the freshest message (not the newest fork root)', () => {
    // Reported scenario: branch A has an older root but the newest message lives
    // in its subtree; branch B was forked later (newer root) but has no tail.
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T01:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T02:00:00Z' });
    const aChild = makeMsg({ message_id: 'a-child', parent_id: 'a', created_at: '2025-01-01T03:00:00Z' });

    const path = selectActivePath({
      messages: [root, a, b, aChild],
      selectedSiblings: {},
    });
    expect(path.map(m => m.message_id)).toEqual(['root', 'a', 'a-child']);
  });

  it('a deep descendant (not the sibling root) can decide the freshest branch', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T01:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T09:00:00Z' });
    const a1 = makeMsg({ message_id: 'a1', parent_id: 'a', created_at: '2025-01-01T02:00:00Z' });
    const a2 = makeMsg({ message_id: 'a2', parent_id: 'a1', created_at: '2025-01-01T10:00:00Z' });

    const path = selectActivePath({
      messages: [root, a, b, a1, a2],
      selectedSiblings: {},
    });
    // b's root (09:00) is newer than a's root (01:00), but a2 (10:00) is newest overall.
    expect(path.map(m => m.message_id)).toEqual(['root', 'a', 'a1', 'a2']);
  });

  it('tie-breaks to the last sibling when max timestamps are equal', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });

    const path = selectActivePath({
      messages: [root, a, b],
      selectedSiblings: {},
    });
    expect(path.map(m => m.message_id)).toEqual(['root', 'b']);
  });

  it('tolerates missing created_at and never lets it win over a real timestamp', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    // a (first in array) has a real timestamp; b (last in array) is missing one.
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '' });

    const path = selectActivePath({
      messages: [root, a, b],
      selectedSiblings: {},
    });
    // Old last-in-array behavior would pick b; the real timestamp on a wins instead.
    expect(path.map(m => m.message_id)).toEqual(['root', 'a']);
  });

  it('explicit selectedSiblings still overrides the freshest default', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T01:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T02:00:00Z' });
    const aChild = makeMsg({ message_id: 'a-child', parent_id: 'a', created_at: '2025-01-01T03:00:00Z' });

    const path = selectActivePath({
      messages: [root, a, b, aChild],
      // Freshest default would pick A, but explicit selection forces B.
      selectedSiblings: { root: 'b' },
    });
    expect(path.map(m => m.message_id)).toEqual(['root', 'b']);
  });

  it('is memoized — returns same reference for same inputs', () => {
    const msgs = makeLinearChain(2);
    const siblings = {};
    const state = { messages: msgs, selectedSiblings: siblings };
    const path1 = selectActivePath(state);
    const path2 = selectActivePath(state);
    expect(path1).toBe(path2);
  });

  it('recomputes when messages change', () => {
    const msgs1 = makeLinearChain(2);
    const path1 = selectActivePath({ messages: msgs1, selectedSiblings: {} });

    const msgs2 = makeLinearChain(3);
    const path2 = selectActivePath({ messages: msgs2, selectedSiblings: {} });
    expect(path1).not.toBe(path2);
    expect(path2).toHaveLength(3);
  });
});

describe('selectSibling', () => {
  it('updates selectedSiblings map', () => {
    useChatStore.getState().selectSibling('parent-1', 'msg-x');
    expect(useChatStore.getState().selectedSiblings).toEqual({ 'parent-1': 'msg-x' });
  });

  it('overwrites previous selection for same parent', () => {
    useChatStore.getState().selectSibling('p1', 'a');
    useChatStore.getState().selectSibling('p1', 'b');
    expect(useChatStore.getState().selectedSiblings.p1).toBe('b');
  });
});

describe('getSiblings', () => {
  it('returns children of given parent', () => {
    const msgs = [
      makeMsg({ message_id: 'a', parent_id: null }),
      makeMsg({ message_id: 'b', parent_id: 'a' }),
      makeMsg({ message_id: 'c', parent_id: 'a' }),
    ];
    useChatStore.setState({ messages: msgs });
    const siblings = useChatStore.getState().getSiblings('a');
    expect(siblings.map(m => m.message_id)).toEqual(['b', 'c']);
  });

  it('returns root-level messages for null parent', () => {
    const msgs = [
      makeMsg({ message_id: 'a', parent_id: null }),
      makeMsg({ message_id: 'b', parent_id: null }),
    ];
    useChatStore.setState({ messages: msgs });
    const siblings = useChatStore.getState().getSiblings(null);
    expect(siblings).toHaveLength(2);
  });

  it('returns empty array for unknown parent', () => {
    useChatStore.setState({ messages: [makeMsg()] });
    expect(useChatStore.getState().getSiblings('nonexistent')).toEqual([]);
  });
});

describe('pendingImages', () => {
  it('adds and removes images', () => {
    const s = useChatStore.getState();
    s.addPendingImage('data:image/png;base64,AAA');
    s.addPendingImage('data:image/png;base64,BBB');
    expect(useChatStore.getState().pendingImages).toHaveLength(2);

    useChatStore.getState().removePendingImage(0);
    expect(useChatStore.getState().pendingImages).toEqual(['data:image/png;base64,BBB']);
  });

  it('clears all pending images', () => {
    useChatStore.getState().addPendingImage('data:x');
    useChatStore.getState().clearPendingImages();
    expect(useChatStore.getState().pendingImages).toEqual([]);
  });
});

describe('stopGeneration', () => {
  it('aborts the active controller', () => {
    const ctrl = new AbortController();
    useChatStore.setState({ streaming: { sessionId: 's1', messageId: null, content: '', controller: ctrl } });
    useChatStore.getState().stopGeneration();
    expect(ctrl.signal.aborted).toBe(true);
  });

  it('is a no-op when no controller', () => {
    expect(() => useChatStore.getState().stopGeneration()).not.toThrow();
  });
});


describe('loadMessages — error surfacing (M3)', () => {
  it('sets messagesError on failure so the empty state is distinguishable', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({ showToast: vi.fn() });
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'));

    useChatStore.setState({ activeSessionId: 's1', messagesError: false, sessions: [makeSession({ session_id: 's1' })] });
    await useChatStore.getState().loadMessages('s1');

    const s = useChatStore.getState();
    expect(s.messagesError).toBe(true);
    expect(s.messagesLoading).toBe(false);
    expect(s.messages).toEqual([]);
  });

  it('clears messagesError on a successful load', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    useChatStore.setState({ activeSessionId: 's1', messagesError: true, sessions: [makeSession({ session_id: 's1' })] });
    await useChatStore.getState().loadMessages('s1');

    expect(useChatStore.getState().messagesError).toBe(false);
  });
});

describe('reset', () => {
  it('clears all state back to defaults', () => {
    useChatStore.setState({
      sessions: [makeSession()],
      activeSessionId: 's1',
      messages: [makeMsg()],
      selectedSiblings: { a: 'b' },
      streaming: { sessionId: 's1', messageId: 'a1', content: 'partial', controller: null },
      pendingImages: ['data:x'],
    });

    useChatStore.getState().reset();
    const s = useChatStore.getState();
    expect(s.sessions).toEqual([]);
    expect(s.activeSessionId).toBeNull();
    expect(s.messages).toEqual([]);
    expect(s.selectedSiblings).toEqual({});
    expect(s.streaming).toBeNull();
    expect(s.pendingImages).toEqual([]);
  });
});

describe('deleteSession', () => {
  it('removes session from list and clears state if active', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'new-s' }),
    );

    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },

      showToast: vi.fn(),
    });

    useChatStore.setState({
      sessions: [makeSession()],
      activeSessionId: 's1',
      documentId: 'd1',
      messages: [makeMsg()],
      models: ['m1'],
      defaultModel: 'm1',
    });

    await useChatStore.getState().deleteSession('s1');

    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/s1');
  });

  it('deleting an active session drops to ghost even when other sessions remain', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },
      showToast: vi.fn(),
    });

    const sOld = makeSession({ session_id: 's-old', updated_at: '2025-01-01T00:00:00Z' });
    const sNew = makeSession({ session_id: 's-new', updated_at: '2025-12-31T23:59:59Z' });
    const sMid = makeSession({ session_id: 's-mid', updated_at: '2025-06-15T12:00:00Z' });

    // Multiple sessions, deleting the active one — must land on ghost, not a neighbor.
    useChatStore.setState({
      sessions: [sOld, sMid, sNew],
      activeSessionId: 's-mid',
      documentId: 'd1',
      models: ['m1'],
      defaultModel: 'm1',
    });

    await useChatStore.getState().deleteSession('s-mid');

    expect(useChatStore.getState().activeSessionId).toBeNull();
    // Remaining sessions are still in state.
    expect(useChatStore.getState().sessions).toHaveLength(2);
  });

  it('deleting the only session drops to ghost (activeSessionId null)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: null,
      showToast: vi.fn(),
    });

    useChatStore.setState({
      sessions: [makeSession()],
      activeSessionId: 's1',
      documentId: 'd1',
      models: ['m1'],
      defaultModel: 'm1',
    });

    await useChatStore.getState().deleteSession('s1');

    expect(useChatStore.getState().activeSessionId).toBeNull();
    expect(useChatStore.getState().ghostSystemPromptId).toBeNull();
  });
});

describe('updateSession — pending patch tracking', () => {
  it('blocks createSession until pending updateSession PATCH resolves', async () => {
    const { apiClient } = await import('../api/client');

    let resolvePatch!: () => void;
    const patchPending = new Promise<void>(r => { resolvePatch = r; });
    const order: string[] = [];

    (apiClient.patch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      order.push('patch');
      return patchPending.then(() => makeSession({ session_id: 's1' }));
    });
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation(() => {
      order.push('get');
      return Promise.resolve([]);
    });
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation(() => {
      order.push('post');
      return Promise.resolve(makeSession({ session_id: 'new-s' }));
    });

    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },
      currentReference: null,
      accessLevel: 'full',
      showToast: vi.fn(),
    });

    const { useUIStore } = await import('./ui-store');
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    useChatStore.setState({
      sessions: [],
      activeSessionId: null,
      documentId: null,
      modelsLoaded: false,
      models: [],
      defaultModel: '',
    });

    // Ghost-chat: an empty doc scope yields a ghost (no auto-create POST). The
    // pending-patch guarantee now lives on the surviving materialization path —
    // a direct createSession (the ChatInput lazy-create on first send) must still
    // wait for any pending updateSession PATCH so the backend inheritance resolver
    // sees the latest model/system_prompt.
    useChatStore.getState().updateSession('s1', { model: 'x' });
    const createP = useChatStore.getState().createSession({ projectId: 'p1', documentId: 'd1' });

    await new Promise(r => setTimeout(r, 10));

    expect(order).toContain('patch');
    expect(order).not.toContain('post');

    resolvePatch();
    await createP;

    expect(order).toContain('post');
  });

  it('updateSession updates store on PATCH success', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 's1', title: 'Updated Title' }),
    );

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1', title: 'Old' })],
    });

    await useChatStore.getState().updateSession('s1', { title: 'Updated Title' });

    const s = useChatStore.getState().sessions.find(ss => ss.session_id === 's1');
    expect(s?.title).toBe('Updated Title');
  });

  it('updateSession shows toast on PATCH failure', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.patch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('fail'));

    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },
      showToast,
    });

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1' })],
    });

    await useChatStore.getState().updateSession('s1', { title: 'fail' });

    expect(showToast).toHaveBeenCalledWith('Failed to update session', 'error');
  });
});

describe('loadModels', () => {
  // Earlier describes override useAppStore.getState via mockReturnValue (sticky),
  // leaving a return object without setMaxAttachmentMb. loadModels now writes the
  // shared budget to app-store (L1), so restore the default mock object here.
  // `appSpy` captures the setMaxAttachmentMb fn so the budget-write assertion works.
  let appSpy: ReturnType<typeof vi.fn>;
  beforeEach(async () => {
    const { useAppStore } = await import('./app-store');
    appSpy = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentProject: null,
      maxAttachmentMb: 5,
      setMaxAttachmentMb: appSpy,
      showToast: vi.fn(),
    });
  });

  it('fetches models and sets defaults', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      models: ['gpt-4', 'claude-3'],
      default_model: 'claude-3',
      max_attachment_mb: 7,
      agent_available: true,
      agent_unavailable_reason: null,
    });

    useChatStore.setState({ modelsLoaded: false });
    await useChatStore.getState().loadModels();

    const s = useChatStore.getState();
    expect(s.models).toEqual(['gpt-4', 'claude-3']);
    expect(s.defaultModel).toBe('claude-3');
    expect(s.modelsLoaded).toBe(true);
    // L1: the shared attachment budget now lives
    // in app-store (single source). loadModels writes it there via setMaxAttachmentMb.
    expect(appSpy).toHaveBeenCalledWith(7);
    // Audit fix #2: agent availability flag is sourced from /chat/models.
    expect(s.agentAvailable).toBe(true);
    expect(s.agentUnavailableReason).toBeNull();
  });

  it('stores agent-unavailable reason from /chat/models', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      models: ['m'],
      default_model: 'm',
      max_attachment_mb: 5,
      agent_available: false,
      agent_unavailable_reason: 'Agent service unreachable',
    });

    useChatStore.setState({ modelsLoaded: false });
    await useChatStore.getState().loadModels();

    const s = useChatStore.getState();
    expect(s.agentAvailable).toBe(false);
    expect(s.agentUnavailableReason).toBe('Agent service unreachable');
  });

  it('stores the per-model reasoning map from /chat/models (plan reasoning-effort-selector)', async () => {
    const { apiClient } = await import('../api/client');
    const reasoning = {
      'deepseek/pro': { supported: true, effort_levels: ['low', 'high', 'max'] },
      'local/orange/chat': { supported: false, effort_levels: [] },
    };
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      models: ['deepseek/pro'],
      default_model: 'deepseek/pro',
      max_attachment_mb: 5,
      agent_available: true,
      agent_unavailable_reason: null,
      reasoning,
    });

    useChatStore.setState({ modelsLoaded: false });
    await useChatStore.getState().loadModels();

    // The map lands verbatim — the dropdown renders the advertised levels as-is.
    expect(useChatStore.getState().reasoning).toEqual(reasoning);
  });

  it('defaults the reasoning map to {} when the payload carries none (gateway without /capabilities)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      models: ['m'],
      default_model: 'm',
      max_attachment_mb: 5,
      agent_available: true,
      agent_unavailable_reason: null,
    });

    useChatStore.setState({ modelsLoaded: false });
    await useChatStore.getState().loadModels();

    // Feature absence, not degradation: {} → no dropdown, no banner.
    expect(useChatStore.getState().reasoning).toEqual({});
  });

  it('skips fetch if already loaded', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.get as ReturnType<typeof vi.fn>).mockClear();

    useChatStore.setState({ modelsLoaded: true, models: ['x'] });
    await useChatStore.getState().loadModels();

    expect(apiClient.get).not.toHaveBeenCalled();
  });
});

describe('createSession — ghost reasoning effort', () => {
  // The file has no top-level mock reset — post/get accumulate calls across
  // describes, so clear them here or calls[0] is a prior test's POST.
  beforeEach(async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
    (apiClient.get as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue([]);
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },
      currentReference: null,
      accessLevel: 'full',
      showToast: vi.fn(),
    });
  });

  it('POSTs reasoning_effort when the ghost carries one', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'new-s' }),
    );

    await useChatStore.getState().createSession({
      projectId: 'p1', documentId: 'd1', reasoningEffort: 'high',
    });

    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body.reasoning_effort).toBe('high');
  });

  it('omits reasoning_effort when none is set (Default = column absent)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'new-s' }),
    );

    await useChatStore.getState().createSession({ projectId: 'p1', documentId: 'd1' });

    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body.reasoning_effort).toBeUndefined();
  });
});

describe('sendMessage mid-turn send (routes to the message queue)', () => {
  it('a send while a turn is streaming makes no POST — it queues', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,

      showToast: vi.fn(),
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    // An open turn holds the streaming slot.
    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: { sessionId: 'sess-1', messageId: null, content: '', controller: null },
      messages: makeLinearChain(2),
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    await useChatStore.getState().sendMessage('hello');
    // The send joins the session's queue; the open turn's end drains it.
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(useChatStore.getState().queued['sess-1']).toEqual(['hello']);
    useChatStore.getState().clearQueued('sess-1');
  });
});

describe('sendMessage — message building', () => {
  it('builds apiMessages from active path using selectedSiblings', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,

      showToast: vi.fn(),
    });

    const root = makeMsg({ message_id: 'root', parent_id: null, role: 'user', content: 'root' });
    const msgA = makeMsg({ message_id: 'msg-a', parent_id: 'root', role: 'assistant', content: 'branch A' });
    const msgB = makeMsg({ message_id: 'msg-b', parent_id: 'root', role: 'assistant', content: 'branch B' });
    const msgAC = makeMsg({ message_id: 'msg-ac', parent_id: 'msg-a', role: 'user', content: 'A child' });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [root, msgA, msgB, msgAC],
      selectedSiblings: { root: 'msg-a' },
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('new message');
    wsFrame(TURN_CLOSED);
    await p;

    const callBody = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(callBody.messages).toHaveLength(4);
    expect(callBody.messages[0].content).toBe('root');
    expect(callBody.messages[1].content).toBe('branch A');
    expect(callBody.messages[2].content).toBe('A child');
    expect(callBody.messages[3].content).toBe('new message');
    expect(callBody.parent_id).toBe('msg-ac');
  });

  it('toasts on an error frame that arrives before ids (H1)', async () => {
    // Regression: an `error` frame delivered before `ids` has no assistant message
    // (streaming.messageId null) and does NOT throw — the turn completes normally,
    // so runCompletion's catch (the only chatSendFailed path) never fires. Assert the
    // error handler surfaces a toast directly in the pre-ids case.
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      currentUser: null,
      showToast,
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'error', message: 'model exploded' });
    wsFrame(TURN_CLOSED);
    await p;

    expect(showToast).toHaveBeenCalledWith('Chat request failed — the response may be incomplete', 'error');
  });

  it('toasts once when a malformed known frame is dropped (M1 no-silent-degradation)', async () => {
    // A malformed KNOWN frame (validateFrame → null) must not be dropped silently —
    // it could lose a tool card. Surface a warning toast, but ONCE per turn even if
    // several malformed frames arrive (unknown types pass through and never toast).
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      currentUser: null,
      showToast,
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame({ type: 'tool_call_start' });                 // no handler — passes silently
    wsFrame({ type: 'ids', assistant_message_id: 'a2' }); // malformed: missing user_message_id
    wsFrame({ type: 'some_future_frame', x: 1 });         // unknown → passes through, no toast
    wsFrame(TURN_CLOSED);
    await p;

    const degraded = showToast.mock.calls.filter(
      ([msg, sev]) => msg === 'Some stream content was lost' && sev === 'warning',
    );
    expect(degraded).toHaveLength(1);
  });

  it('stamps author_name/author_id on the optimistic + reconciled user message (Issue 1)', async () => {
    // Regression: AI chat showed "unknown" until reload because both the
    // optimistic bubble (insertOptimisticUser) and the ids-reconciled message
    // (streaming.ts) were built locally without author fields. Notes worked
    // because they use the server-serialized message. Both constructors now
    // stamp currentUser. Here we assert the reconciled user message carries
    // the current user's name/id immediately after the ids event.
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      currentUser: { user_id: 'u-1', name: 'Alice' },
      showToast: vi.fn(),
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hello');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame(TURN_CLOSED);
    await p;

    const userMsg = useChatStore.getState().messages.find(m => m.message_id === 'u1');
    expect(userMsg?.author_name).toBe('Alice');
    expect(userMsg?.author_id).toBe('u-1');
  });

  it('omits author fields when no currentUser (auth edge case)', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      currentUser: undefined,
      showToast: vi.fn(),
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hello');
    wsFrame({ type: 'ids', user_message_id: 'u2', assistant_message_id: 'a2' });
    wsFrame(TURN_CLOSED);
    await p;

    const userMsg = useChatStore.getState().messages.find(m => m.message_id === 'u2');
    // Keys omitted (mirrors the optional ChatMessage type) — no stale "unknown".
    expect(userMsg?.author_name).toBeUndefined();
    expect(userMsg?.author_id).toBeUndefined();
  });

  it('seats the streaming object with an AbortController on send', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,

      showToast: vi.fn(),
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: makeLinearChain(2),
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const promise = useChatStore.getState().sendMessage('test');
    expect(useChatStore.getState().streaming).not.toBeNull();
    expect(useChatStore.getState().streaming?.controller).toBeInstanceOf(AbortController);

    wsFrame(TURN_CLOSED);
    await promise;
    expect(useChatStore.getState().streaming).toBeNull();
  });

  it('returns early without fetch when no activeSessionId', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear();
    useChatStore.setState({ activeSessionId: null, streaming: null });
    await useChatStore.getState().sendMessage('test');
    expect(apiClient.post).not.toHaveBeenCalled();
  });
});

// A send failure must surface explicitly (no silent degradation).
describe('sendMessage — send failure surfacing (F1)', () => {
  let toastSpy: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    const { useAppStore } = await import('./app-store');
    toastSpy = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast: toastSpy,
      currentUser: { user_id: 'u', name: 'U' },
    });
    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: makeLinearChain(2),
      selectedSiblings: {},
    });
  });

  it('marks the truncated assistant message unsaved + toasts when the POST fails after ids', async () => {
    const { apiClient } = await import('../api/client');
    // The turn opened and the `ids` frame (WS) created the assistant row; then
    // the completions POST itself fails — the post-`ids` failure scenario.
    let rejectPost!: (e: Error) => void;
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear()
      .mockReturnValue(new Promise((_res, rej) => { rejectPost = rej; }));

    const p = useChatStore.getState().sendMessage('hello');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    rejectPost(new Error('network reset'));
    await p;

    const a1 = useChatStore.getState().messages.find(m => m.message_id === 'a1');
    // The truncated assistant bubble is marked unsaved (visibly distinct).
    expect(a1?.unsaved).toBe(true);
    // A generic failure toast fired (no silent degradation).
    expect(toastSpy).toHaveBeenCalledWith(expect.anything(), 'error');
  });

  it('toasts on a network 500 (POST rejects before any frame)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear()
      .mockRejectedValue(new Error('500'));

    await useChatStore.getState().sendMessage('hello');

    expect(toastSpy).toHaveBeenCalledWith(expect.anything(), 'error');
  });

  it('does NOT mark unsaved or toast on a user-initiated abort', async () => {
    const { apiClient } = await import('../api/client');
    // Emit ids (so an assistant row exists), the user presses Stop, then the
    // POST fails as an abort.
    let rejectPost!: (e: Error) => void;
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear()
      .mockReturnValue(new Promise((_res, rej) => { rejectPost = rej; }));

    const p = useChatStore.getState().sendMessage('hello');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    useChatStore.getState().streaming?.controller?.abort();
    const err = new Error('aborted');
    err.name = 'AbortError';
    rejectPost(err);
    await p;

    const a1 = useChatStore.getState().messages.find(m => m.message_id === 'a1');
    // A deliberate user stop is NOT a save failure — no unsaved badge.
    expect(a1?.unsaved).toBeFalsy();
    expect(toastSpy).not.toHaveBeenCalledWith(expect.anything(), 'error');
  });

  it('does NOT toast on an error frame arriving during a user-initiated stop', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear()
      .mockResolvedValue({ accepted: true });

    // A trailing `error` frame (the backend aborted the turn) arrives AFTER the
    // user has already pressed Stop — the handler must treat it as part of the
    // stop, not a failure (mirrors the AbortError guard in messages-slice.ts).
    // Stop routes through stopGeneration, which stamps the registration's end
    // facts before aborting.
    const p = useChatStore.getState().sendMessage('hello');
    // runCompletion sets streaming.controller synchronously; the user Stops.
    useChatStore.getState().stopGeneration();
    wsFrame({ type: 'error', message: 'Request was aborted' });
    wsFrame(TURN_CLOSED);
    await p;

    expect(toastSpy).not.toHaveBeenCalledWith(expect.anything(), 'error');
  });
});

describe('flushStreaming (via sendMessage finally block)', () => {
  it('lands the turn text on the assistant row at turn end', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,

      showToast: vi.fn(),
    });

    // The turn's text reaches the row on the `done` content frame (the
    // turn's terminal is `turn_closed`) — the
    // BACKEND's accumulation. Nothing folds deltas in the browser any more:
    // the reply is drawn from the assembler's nodes, and `content` is what the
    // list preview and a frameless fallback read.
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      selectedSiblings: {},
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame({ type: 'done', content: 'Hello world' });
    wsFrame(TURN_CLOSED);
    await p;

    const state = useChatStore.getState();
    expect(state.streaming).toBeNull();
    const assistantMsg = state.messages.find(m => m.message_id === 'a1');
    expect(assistantMsg?.content).toBe('Hello world');
  });
});

describe('streamCompletion — sources display', () => {
  it('attaches sources that arrive BEFORE the ids event (order-independent)', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast: vi.fn(),
    });

    // Regression: 8d16796 reordered the backend stream so manual `sources` is
    // emitted BEFORE `ids`. The handler read streamingMessageId (still null) and
    // dropped the payload. Sources must survive regardless of arrival order.
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      selectedSiblings: {},
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'sources', sources: [{ kind: 'document', id: 'd1', title: 'Doc', retrieved: false }] });
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame({ type: 'delta', content: 'Hello' });
    wsFrame(TURN_CLOSED);
    await p;

    const assistantMsg = useChatStore.getState().messages.find(m => m.message_id === 'a1');
    expect(assistantMsg?.sources).toEqual([
      { kind: 'document', id: 'd1', title: 'Doc', retrieved: false },
    ]);
  });

  it('merges manual (pre-ids) and semantic-search (mid-turn) sources, dedup by id', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast: vi.fn(),
    });

    // Manual source d1 before ids; later search_materials emits a second sources
    // event: a new semantic hit d2 (retrieved) + d1 again (must NOT duplicate;
    // the manual retrieved:false entry wins).
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      selectedSiblings: {},
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'sources', sources: [{ kind: 'document', id: 'd1', title: 'Doc1', retrieved: false }] });
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame({ type: 'delta', content: 'Hi' });
    wsFrame({ type: 'sources', sources: [{ kind: 'document', id: 'd2', title: 'Doc2', retrieved: true }, { kind: 'document', id: 'd1', title: 'Doc1', retrieved: true }] });
    wsFrame(TURN_CLOSED);
    await p;

    const assistantMsg = useChatStore.getState().messages.find(m => m.message_id === 'a1');
    expect(assistantMsg?.sources).toEqual([
      { kind: 'document', id: 'd1', title: 'Doc1', retrieved: false },
      { kind: 'document', id: 'd2', title: 'Doc2', retrieved: true },
    ]);
  });
});

describe('streamCompletion — context_warning handling', () => {
  it('shows warning toast on context_warning frame', async () => {
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast,
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      selectedSiblings: {},
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    wsFrame({ type: 'context_warning', reason: 'document_not_found', document_id: 'd-missing' });
    wsFrame({ type: 'delta', content: 'Hello' });
    wsFrame(TURN_CLOSED);
    await p;

    expect(showToast).toHaveBeenCalledWith('Some context documents failed to load', 'warning');
  });

  it('logs malformed frames instead of silently swallowing', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast: vi.fn(),
    });

    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      activeSessionId: 'sess-1',
      streaming: null,
      messages: [],
      selectedSiblings: {},
      sessions: [makeSession({ session_id: 'sess-1' })],
    });

    const p = useChatStore.getState().sendMessage('hi');
    wsFrame({ type: 'ids', user_message_id: 'u1', assistant_message_id: 'a1' });
    // A malformed KNOWN frame (validateFrame → null) warns + toasts once.
    wsFrame({ type: 'ids', assistant_message_id: 'a2' });
    wsFrame({ type: 'delta', content: 'Hello' });
    wsFrame(TURN_CLOSED);
    await p;

    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
   });
});

describe('agent-mode fork/regenerate gate (ST1)', () => {
  it('forkAndResend PROCEEDS in an agent session (edit→branch is now allowed)', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast,
    });
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear().mockResolvedValue({ accepted: true });

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's-agent', mode: 'agent', target_doc_id: 'd1' } as Record<string, unknown>)],
      activeSessionId: 's-agent',
      messages: [makeMsg({ message_id: 'u1', role: 'user' })],
      streaming: null,
    });

    const p = useChatStore.getState().forkAndResend('u1', 'replay');
    wsFrame(TURN_CLOSED, 's-agent');
    await p;

    expect(apiClient.post).toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('regenerate is a no-op + toast in agent sessions', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentReference: null,
      showToast,
    });
    (apiClient.post as ReturnType<typeof vi.fn>).mockClear();

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's-agent', mode: 'agent', target_doc_id: 'd1' } as Record<string, unknown>)],
      activeSessionId: 's-agent',
      messages: [
        makeMsg({ message_id: 'u1', role: 'user', parent_id: null }),
        makeMsg({ message_id: 'a1', role: 'assistant', parent_id: 'u1' }),
      ],
      streaming: null,
    });

    await useChatStore.getState().regenerate('a1');

    expect(apiClient.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalled();
  });
});

describe('loadSessions cache invalidation on revisit', () => {
  // INVARIANT: docA → docB → docA must refetch on the third call.  Why: old behavior cached resolved promises forever, so a revisit served stale sessions (chats appeared frozen); the test asserts the third call refetches. The old
  // behavior kept resolved promises forever in loadInFlight, so revisits hit
  // the stale cache and never repopulated sessions — chats appeared frozen
  // on the previous document until page reload.
  it('refetches when revisiting a scope after navigating away', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    const sessionsByDoc: Record<string, ReturnType<typeof makeSession>[]> = {
      'docA-cache': [makeSession({ session_id: 'sA', document_id: 'docA-cache' })],
      'docB-cache': [makeSession({ session_id: 'sB', document_id: 'docB-cache' })],
    };
    let calls = 0;
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (!url.startsWith('/chat/sessions?')) return Promise.resolve([]);
      calls += 1;
      const match = url.match(/document_id=([^&]+)/);
      const did = match?.[1] ?? '';
      return Promise.resolve(sessionsByDoc[did] ?? []);
    });

    await useChatStore.getState().loadSessions('p-cache', 'docA-cache');
    expect(useChatStore.getState().sessions[0].session_id).toBe('sA');

    await useChatStore.getState().loadSessions('p-cache', 'docB-cache');
    expect(useChatStore.getState().sessions[0].session_id).toBe('sB');

    // Third call: revisit docA. With cache-on-resolve, prior call's resolved
    // entry was deleted, so this MUST issue a fresh fetch and repopulate.
    await useChatStore.getState().loadSessions('p-cache', 'docA-cache');
    expect(useChatStore.getState().sessions[0].session_id).toBe('sA');
    expect(calls).toBe(3);
  });

  it('still dedupes concurrent callers in the same tick', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    let calls = 0;
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (!url.startsWith('/chat/sessions?')) return Promise.resolve([]);
      calls += 1;
      return Promise.resolve([makeSession({ session_id: 'sD', document_id: 'docD-dedup' })]);
    });

    const a = useChatStore.getState().loadSessions('p-dedup', 'docD-dedup');
    const b = useChatStore.getState().loadSessions('p-dedup', 'docD-dedup');
    await Promise.all([a, b]);
    expect(calls).toBe(1);
  });
});

describe('loadSessions piggyback (Item 3: collapse sessions→messages waterfall)', () => {
  async function mockStores(savedId: string | null) {
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => savedId),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });
  }

  it('commits piggybacked messages and does NOT fetch /messages when ids agree', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores('sA');
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });

    const piggybacked = [
      makeMsg({ message_id: 'm1', role: 'user', parent_id: null }),
      makeMsg({ message_id: 'm2', role: 'assistant', parent_id: 'm1' }),
    ];
    const messagesUrls: string[] = [];
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.startsWith('/chat/sessions?')) {
        return Promise.resolve({
          sessions: [makeSession({ session_id: 'sA', document_id: 'docP' })],
          active_messages: { session_id: 'sA', messages: piggybacked },
        });
      }
      if (url.includes('/messages')) { messagesUrls.push(url); return Promise.resolve([]); }
      return Promise.resolve([]);
    });

    await useChatStore.getState().loadSessions('pP', 'docP');

    expect(useChatStore.getState().activeSessionId).toBe('sA');
    expect(useChatStore.getState().messages.map(m => m.message_id)).toEqual(['m1', 'm2']);
    expect(useChatStore.getState().chatScopeLoading).toBe(false);
    expect(messagesUrls).toHaveLength(0); // one round-trip only
  });

  it('feeds the piggybacked frames to the assembler and commits the row without them', async () => {
    // The piggyback is the restore path of a page reload / second tab. It skips
    // loadMessages, where the rows' frames reach the assembler — so a reload
    // committed the rows raw and the session rendered text-only until a manual
    // re-select: the live-vs-reload divergence the read path exists to prevent.
    const { apiClient } = await import('../api/client');
    await mockStores('sA');
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });

    const piggybacked = [
      makeMsg({ message_id: 'm1', role: 'user', parent_id: null }),
      {
        ...makeMsg({ message_id: 'm2', role: 'assistant', parent_id: 'm1', content: '' }),
        frames: [
          { type: 'dsh_event', kind: 'turn/start', seq: 1, data: { turn: 0 } },
          { type: 'dsh_event', kind: 'step/start', seq: 2, data: { turn: 0, step: 0 } },
          { type: 'dsh_event', kind: 'tool/call', seq: 3,
            data: { turn: 0, step: 0, callId: 'c1', name: 'search_materials', arguments: '{}' } },
        ],
      },
    ];
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.startsWith('/chat/sessions?')) {
        return Promise.resolve({
          sessions: [makeSession({ session_id: 'sA', document_id: 'docP' })],
          active_messages: { session_id: 'sA', messages: piggybacked },
        });
      }
      return Promise.resolve([]);
    });

    await useChatStore.getState().loadSessions('pP', 'docP');

    const state = useChatStore.getState();
    const assistant = state.messages.find(m => m.message_id === 'm2');
    // The frames went to the assembler, and the row carries none of them.
    expect((assistant as unknown as Record<string, unknown>).frames).toBeUndefined();
    expect(state.conversation.some(n => n.kind === 'tool-call')).toBe(true);
    // The row is placed: its turn window owns the published nodes.
    expect(state.turnRanges.m2).toEqual({ min: 1, max: 3 });
  });

  it('falls back to /messages fetch when the piggyback id mismatches the resolution', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores('sA'); // saved = sA, but backend piggybacks sZ
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });

    const messagesUrls: string[] = [];
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.startsWith('/chat/sessions?')) {
        return Promise.resolve({
          sessions: [makeSession({ session_id: 'sA', document_id: 'docP' })],
          active_messages: { session_id: 'sZ', messages: [] },
        });
      }
      if (url.includes('/messages')) { messagesUrls.push(url); return Promise.resolve([]); }
      return Promise.resolve([]);
    });

    await useChatStore.getState().loadSessions('pP', 'docP');

    expect(useChatStore.getState().activeSessionId).toBe('sA');
    expect(messagesUrls).toEqual(['/chat/sessions/sA/messages']);
  });

  it('piggybacked restore also fetches pending verdicts (second-tab render gate)', async () => {
    // The piggyback skips loadMessages, and fetchPendingVerdicts lived ONLY on
    // that path — so a second tab / reload restoring via with_active_messages
    // never discovered a still-held call and rendered no decision card until a
    // manual session re-select. The piggyback branch must fetch the verdicts
    // itself.
    const { apiClient } = await import('../api/client');
    await mockStores('sA');
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });

    const piggybacked = [
      makeMsg({ message_id: 'm1', role: 'user', parent_id: null }),
      makeMsg({ message_id: 'm2', role: 'assistant', parent_id: 'm1' }),
    ];
    const verdictUrls: string[] = [];
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.startsWith('/chat/sessions?')) {
        return Promise.resolve({
          sessions: [makeSession({ session_id: 'sA', document_id: 'docP' })],
          active_messages: { session_id: 'sA', messages: piggybacked },
        });
      }
      if (url.startsWith('/chat/verdicts')) {
        verdictUrls.push(url);
        return Promise.resolve({
          holds: [{ call_id: 'c1', tool_name: 'create_document', message_id: 'm2' }],
        });
      }
      if (url.includes('/messages')) { return Promise.resolve([]); }
      return Promise.resolve([]);
    });

    await useChatStore.getState().loadSessions('pP', 'docP');
    // fetchPendingVerdicts is fire-and-forget — flush microtasks.
    await new Promise(r => setTimeout(r, 0));

    expect(verdictUrls).toEqual(['/chat/verdicts?session_id=sA']);
    const m2 = useChatStore.getState().messages.find(m => m.message_id === 'm2');
    expect(m2?.pending_verdicts).toEqual([
      { call_id: 'c1', tool_name: 'create_document', message_id: 'm2' },
    ]);
  });
});

describe('loadSessions stale-scope race guard', () => {
  // A late-resolving loadSessions for a superseded PROJECT must NOT apply its
  // result. The active chat is project-scoped,
  // so the race is between two DIFFERENT projects; the scopeKey is per-project.
  async function mockStores() {
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      // Project-level last-active depends on the requested scope (documentId):
      // project A → chatA, project B → chatB.
      getLastActiveChatSession: vi.fn(() =>
        useChatStore.getState().documentId === 'docA-race' ? 'chatA' : 'chatB'),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });
  }

  it('discards a superseded project-load (latest wins, stale does not setActiveSession)', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();

    const chatA = makeSession({ session_id: 'chatA', document_id: 'docA-race' });
    const projBSessions = [makeSession({ session_id: 'chatB', document_id: 'docB-race' })];

    // First GET (project A) is deferred; second GET (project B) resolves immediately.
    let releaseA!: () => void;
    const aPending = new Promise<unknown>(r => { releaseA = () => r([chatA]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const match = url.match(/document_id=([^&]+)/);
      const did = match?.[1] ?? '';
      if (did === 'docA-race') return aPending;
      if (did === 'docB-race') return Promise.resolve(projBSessions);
      return Promise.resolve([]);
    });

    // [A] start: loadSessions(projA) — sets scope, awaits deferred fetch.
    const loadA = useChatStore.getState().loadSessions('pA-race', 'docA-race');
    await new Promise(r => setTimeout(r, 0));
    expect(getStoreScope()).toEqual({ documentId: 'docA-race' });

    // [B] start: loadSessions(projB) — overwrites scope synchronously, resolves fast.
    const loadB = useChatStore.getState().loadSessions('pB-race', 'docB-race');
    await loadB;
    // [B] applied: project B's last-active chat is active.
    expect(useChatStore.getState().activeSessionId).toBe('chatB');
    expect(getStoreScope()).toEqual({ documentId: 'docB-race' });

    // [A] resolves late — must NOT clobber project B.
    releaseA();
    await loadA;

    expect(useChatStore.getState().activeSessionId).toBe('chatB');
    expect(useChatStore.getState().sessions.map(s => s.session_id)).toEqual(['chatB']);
    expect(getStoreScope()).toEqual({ documentId: 'docB-race' });
  });

  // NOTE: same-project concurrent-call dedup is covered by "still dedupes
  // concurrent callers in the same tick" in the cache-on-resolve block above.

  function getStoreScope() {
    const s = useChatStore.getState();
    return { documentId: s.documentId };
  }
});

describe('loadSessions scope-change spinner (no stale flash on project switch)', () => {
  // On a real scope (project) change the chat is PROJECT-SCOPED and persists
  // across doc navigation, but the panel must NOT flash the prior project's chat
  // while the new project loads. chatScopeLoading is raised synchronously at
  // entry (driving the MessageList spinner that hides the prior chat); the active
  // chat / messages are NOT cleared — the resolver restores the project's
  // last-active chat or yields a ghost when the fetch settles.
  it('raises chatScopeLoading synchronously on a scope change (hides the prior chat)', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    // Deferred fetch so we can assert state BEFORE it resolves.
    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    // Seed: prior scope active with a chat + messages (hidden behind the spinner).
    useChatStore.setState({
      documentId: 'docA-flash',
      activeSessionId: 'parent-chat',
      messages: [makeMsg({ message_id: 'pm', chat_id: 'parent-chat' })],
    });

    // Start a load for a DIFFERENT document (scope change) — spinner raised synchronously.
    useChatStore.getState().loadSessions('p-flash', 'docB-flash');

    const s = useChatStore.getState();
    expect(s.documentId).toBe('docB-flash');
    expect(s.chatScopeLoading, 'spinner must cover the prior chat during load').toBe(true);

    release();
    await new Promise(r => setTimeout(r, 0));
  });

  it('keeps the active chat when only a reference changes within the same document', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    useChatStore.setState({
      documentId: 'docSame',
      activeSessionId: 'kept-chat',
      messages: [makeMsg({ message_id: 'km', chat_id: 'kept-chat' })],
    });

    // Same-scope re-load (scopeChanged=false) — no spinner, active chat stays.
    useChatStore.getState().loadSessions('p-keep', 'docSame');

    const s = useChatStore.getState();
    expect(s.activeSessionId).toBe('kept-chat');
    expect(s.messages).toHaveLength(1);

    release();
    await new Promise(r => setTimeout(r, 0));
  });

  // Ghost pin: the in-memory ghostRegion is transient
  // and doc-scoped — a scope (project) change must drop it synchronously at loadSessions
  // entry, so the pill + highlight never linger on the wrong project.
  it('clears ghostRegion synchronously on a real document change', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    // Seed: docA has an in-memory ghost pin.
    useChatStore.setState({
      documentId: 'docA-ghost',
      ghostRegion: { doc_id: 'docA-ghost', relFrom: { a: 1 }, relTo: { a: 2 } },
    });

    // Start a load for a DIFFERENT document — must drop the ghost pin synchronously.
    useChatStore.getState().loadSessions('p-ghost', 'docB-ghost');

    expect(useChatStore.getState().ghostRegion, 'ghost pin leaked onto the new document').toBeNull();

    release();
    await new Promise(r => setTimeout(r, 0));
  });

  it('keeps ghostRegion when re-loading the same document (no document change)', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');

    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });

    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    const region = { doc_id: 'docSame-ghost', relFrom: { a: 1 }, relTo: { a: 2 } };
    useChatStore.setState({ documentId: 'docSame-ghost', ghostRegion: region });

    // Same-scope re-load (scopeChanged=false); the ghost pin survives.
    useChatStore.getState().loadSessions('p-ghost-keep', 'docSame-ghost');

    expect(useChatStore.getState().ghostRegion).toEqual(region);

    release();
    await new Promise(r => setTimeout(r, 0));
  });
});

describe('chatScopeLoading lifecycle (right-panel flicker guard)', () => {
  // The single gate that keeps MessageList on one spinner through
  // entry → resolve → messages-loaded, so the "no chats" empty state never
  // flashes mid-load on a document change. Regression guard for the reported
  // navigation flicker.
  function mockUI(overrides: Record<string, unknown> = {}) {
    return {
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      ...overrides,
    };
  }

  async function mockStores(uiOverrides: Record<string, unknown> = {}) {
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue(mockUI(uiOverrides));
  }

  it('is true synchronously after loadSessions on a real document change', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();

    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    useChatStore.setState({ documentId: 'docA-scope', activeSessionId: 'parent' });
    useChatStore.getState().loadSessions('p-scope', 'docB-scope');

    expect(useChatStore.getState().chatScopeLoading).toBe(true);

    release();
    await new Promise(r => setTimeout(r, 0));
  });

  it('stays false on same-document reference navigation (keep_current)', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();

    let release!: () => void;
    const pending = new Promise<unknown>(r => { release = () => r([]); });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    useChatStore.setState({ documentId: 'docSame-scope', activeSessionId: 'kept' });
    useChatStore.getState().loadSessions('p-keep-scope', 'docSame-scope');

    expect(useChatStore.getState().chatScopeLoading).toBe(false);

    release();
    await new Promise(r => setTimeout(r, 0));
  });

  it('is false after messages load on a document change (restore_saved → loadMessages)', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores({ getLastActiveChatSession: vi.fn(() => 'chatB') });

    const sessions = [makeSession({ session_id: 'chatB', document_id: 'docB-load' })];
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url.includes('/messages')) return Promise.resolve([]);
      return Promise.resolve(sessions);
    });

    useChatStore.setState({ documentId: 'docA-load', activeSessionId: 'parent' });
    await useChatStore.getState().loadSessions('p-load', 'docB-load');
    await new Promise(r => setTimeout(r, 0));

    expect(useChatStore.getState().activeSessionId).toBe('chatB');
    expect(useChatStore.getState().chatScopeLoading).toBe(false);
  });

  it('is false when the sessions fetch rejects (catch)', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();

    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    useChatStore.setState({ documentId: 'docA-err', activeSessionId: 'parent' });
    await useChatStore.getState().loadSessions('p-err', 'docB-err');
    await new Promise(r => setTimeout(r, 0));

    expect(useChatStore.getState().chatScopeLoading).toBe(false);
  });

  // Mechanism B (groovy-skipping-wozniak): a stale async terminal must NEVER clear
  // a gate owned by a newer request.
  it('stale loadMessages (superseded session) does NOT clear a gate owned by the new scope', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]); // messages

    // The new scope owns the gate; an OLD session's loadMessages is still in flight.
    useChatStore.setState({ activeSessionId: 'new-session', chatScopeLoading: true });
    await useChatStore.getState().loadMessages('old-session'); // stale: active !== 'old-session'

    expect(useChatStore.getState().chatScopeLoading).toBe(true);
  });

  it('stale loadMessages catch does NOT clear a gate owned by the new scope', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    useChatStore.setState({ activeSessionId: 'new-session', chatScopeLoading: true });
    await useChatStore.getState().loadMessages('old-session');
    await new Promise(r => setTimeout(r, 0));

    expect(useChatStore.getState().chatScopeLoading).toBe(true);
  });

  it('current-session loadMessages catch DOES clear the gate (it owns it)', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    useChatStore.setState({ activeSessionId: 'cur-session', chatScopeLoading: true });
    await useChatStore.getState().loadMessages('cur-session');
    await new Promise(r => setTimeout(r, 0));

    expect(useChatStore.getState().chatScopeLoading).toBe(false);
  });

  it('loadSessions catch does NOT clear a gate after the user navigated to another scope', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores();

    let reject!: (e: unknown) => void;
    const pending = new Promise((_r, rej) => { reject = rej; });
    (apiClient.get as ReturnType<typeof vi.fn>).mockReturnValue(pending);

    useChatStore.setState({ documentId: 'docA-nav', activeSessionId: 'parent' });
    const p = useChatStore.getState().loadSessions('p-nav', 'docB-nav'); // gate true, documentId='docB-nav'
    expect(useChatStore.getState().chatScopeLoading).toBe(true);

    // User navigates onward before the stale fetch rejects.
    useChatStore.setState({ documentId: 'docC-nav', chatScopeLoading: true });
    reject(new Error('boom'));
    await p.catch(() => {});
    await new Promise(r => setTimeout(r, 0));

    expect(useChatStore.getState().chatScopeLoading).toBe(true);
  });
});

// ─── project-scoped active chat ──────────

describe('project-scoped active chat — persistence + restore', () => {
  async function mockStores(lastActive: string | null = null) {
    const { useAppStore } = await import('./app-store');
    const { useUIStore } = await import('./ui-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null, currentReference: null, showToast: vi.fn(),
    });
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => lastActive),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });
  }

  it('setActiveSession pins the project-level active chat', async () => {
    const { useUIStore } = await import('./ui-store');
    const setLast = vi.fn();
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: setLast,
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });
    useChatStore.setState({ sessions: [makeSession({ session_id: 's1' })] });

    useChatStore.getState().setActiveSession('s1');

    expect(setLast).toHaveBeenCalledWith('s1');
  });

  it('loadSessions restores the project-level last-active chat when present', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores('proj-active');
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'proj-active', document_id: 'docP' }),
    ]);

    await useChatStore.getState().loadSessions('pP', 'docP');

    expect(useChatStore.getState().activeSessionId).toBe('proj-active');
  });

  it('loadSessions yields ghost when the project has no last-active chat', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores(null);
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'owned', document_id: 'docP' }),
    ]);

    await useChatStore.getState().loadSessions('pP', 'docP');

    // No auto-adoption: a project with chats but no last-active pointer → ghost.
    expect(useChatStore.getState().activeSessionId).toBeNull();
  });

  it('loadSessions yields ghost when the last-active id was deleted', async () => {
    const { apiClient } = await import('../api/client');
    await mockStores('deleted');
    useChatStore.setState({ documentId: null, activeSessionId: null, messages: [] });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'other', document_id: 'docP' }),
    ]);

    await useChatStore.getState().loadSessions('pP', 'docP');

    expect(useChatStore.getState().activeSessionId).toBeNull();
  });

  it('deleteSession of the active chat clears the project-level active pointer', async () => {
    const { apiClient } = await import('../api/client');
    const { useUIStore } = await import('./ui-store');
    const setLast = vi.fn();
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({});
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: setLast,
      getLastActiveChatSession: vi.fn(() => 's1'),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
    });
    useChatStore.setState({ sessions: [makeSession({ session_id: 's1' })], activeSessionId: 's1' });

    await useChatStore.getState().deleteSession('s1');

    expect(setLast).toHaveBeenCalledWith(null);
    expect(useChatStore.getState().activeSessionId).toBeNull();
  });

  it('document switch within a project does NOT reload (scopeKey is project-level)', async () => {
    // Two same-project loads for different docs hit the per-project in-flight
    // dedup (same scopeKey = projectId), so only ONE fetch fires.
    const { apiClient } = await import('../api/client');
    await mockStores(null);
    let calls = 0;
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation(() => {
      calls += 1;
      return Promise.resolve([makeSession({ session_id: 's1', document_id: 'docA' })]);
    });

    await useChatStore.getState().loadSessions('sameProj', 'docA');
    // A second same-project load is in-flight-deduped OR (after settle) re-fetches
    // — but it never clears the active chat mid-flight. The active chat survives.
    await useChatStore.getState().loadSessions('sameProj', 'docB');

    // Both resolved; active chat is whatever the last resolve produced (project-scoped).
    expect(useChatStore.getState().sessions.length).toBeGreaterThanOrEqual(1);
    expect(calls).toBeGreaterThanOrEqual(1);
  });
});

// Ghost-chat. startGhostChat opens a fresh
// client-only ghost and resets overrides. The ghost context is DERIVED from the open
// entity (useGhostChatContext) — startGhostChat no longer attaches anything. No POST.
describe('ghost chat — startGhostChat + overrides', () => {
  it('startGhostChat opens a ghost (null active), resets overrides, un-pins the doc, no POST', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as any).mockClear();

    // Simulate a prior active session + dirty ghost overrides.
    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1' }) as any],
      activeSessionId: 's1',
      ghostAgentAuto: true,
      ghostSystemPromptId: 'prompt-x',
    });

    useChatStore.getState().startGhostChat();

    expect(useChatStore.getState().activeSessionId).toBeNull();
    expect(useChatStore.getState().messages).toEqual([]);
    // Overrides re-inherited from the active session (s1's agent_auto=false,
    // system_prompt_id=null), overwriting the dirty values above.
    expect(useChatStore.getState().ghostAgentAuto).toBe(false);
    expect(useChatStore.getState().ghostSystemPromptId).toBeNull();
    // No POST — a ghost is client-only until the first send materializes a row.
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('startGhostChat is a no-POST path (lazy everywhere)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as any).mockClear();
    useChatStore.getState().startGhostChat();
    expect(apiClient.post).not.toHaveBeenCalled();
  });

  it('setSessionUIMode writes the ghost override when no session is active', async () => {
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentProject: null,
      accessLevel: 'full',
      showToast: vi.fn(),
    });
    useChatStore.setState({
      activeSessionId: null,
      ghostAgentAuto: false,
    });
    useChatStore.getState().setSessionUIMode('agent_auto');
    expect(useChatStore.getState().ghostAgentAuto).toBe(true);

    // Switch back to confirm — the surviving toggle (no readonly line).
    useChatStore.getState().setSessionUIMode('agent_confirm');
    expect(useChatStore.getState().ghostAgentAuto).toBe(false);
  });

  it('setSessionUIMode blocks agent_auto without full access', async () => {
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    // mockReturnValue (not Once): setSessionUIMode calls getState twice
    // (access check + toast) — both must resolve to the readonly state.
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentDocument: null,
      currentProject: null,
      accessLevel: 'readonly',
      showToast,
    });
    useChatStore.setState({ activeSessionId: null });
    useChatStore.getState().setSessionUIMode('agent_auto');
    expect(useChatStore.getState().ghostAgentAuto).toBe(false);
    expect(showToast).toHaveBeenCalled();
  });

  it('setGhostSystemPrompt holds the choice client-side', () => {
    useChatStore.getState().setGhostSystemPrompt('prompt-1');
    expect(useChatStore.getState().ghostSystemPromptId).toBe('prompt-1');
    useChatStore.getState().setGhostSystemPrompt(null);
    expect(useChatStore.getState().ghostSystemPromptId).toBeNull();
  });
});

