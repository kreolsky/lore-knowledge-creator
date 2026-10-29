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

import { useChatStore, selectBranchPath } from './chat-store';
import { dispatchChatFrame } from './chat-store/streaming';
import { clearChatCaches } from './chat-store/reset-registry';
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

describe('selectBranchPath — the branch path', () => {

  it('a deep descendant (not the sibling root) can decide the freshest branch', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T01:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T09:00:00Z' });
    const a1 = makeMsg({ message_id: 'a1', parent_id: 'a', created_at: '2025-01-01T02:00:00Z' });
    const a2 = makeMsg({ message_id: 'a2', parent_id: 'a1', created_at: '2025-01-01T10:00:00Z' });

    const path = selectBranchPath({ messages: [root, a, b, a1, a2] });
    // b's root (09:00) is newer than a's root (01:00), but a2 (10:00) is newest overall.
    expect(path.map(m => m.message_id)).toEqual(['root', 'a', 'a1', 'a2']);
  });

  it('tie-breaks to the last sibling when max timestamps are equal', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });

    const path = selectBranchPath({ messages: [root, a, b] });
    expect(path.map(m => m.message_id)).toEqual(['root', 'b']);
  });

  it('tolerates missing created_at and never lets it win over a real timestamp', () => {
    const root = makeMsg({ message_id: 'root', parent_id: null, created_at: '2025-01-01T00:00:00Z' });
    // a (first in array) has a real timestamp; b (last in array) is missing one.
    const a = makeMsg({ message_id: 'a', parent_id: 'root', created_at: '2025-01-01T05:00:00Z' });
    const b = makeMsg({ message_id: 'b', parent_id: 'root', created_at: '' });

    const path = selectBranchPath({ messages: [root, a, b] });
    // Old last-in-array behavior would pick b; the real timestamp on a wins instead.
    expect(path.map(m => m.message_id)).toEqual(['root', 'a']);
  });
});

describe('stopGeneration', () => {

  it('is a no-op when no controller', () => {
    expect(() => useChatStore.getState().stopGeneration()).not.toThrow();
  });
});

describe('deleteSession — an AI chat is a thread', () => {
  it('deleting from an open branch DELETEs the thread root and drops every row of the thread', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockClear();
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'p1' },
      currentDocument: { document_id: 'd1' },
      showToast: vi.fn(),
    });

    // The list previews the thread by its open branch (the server's display
    // row); a stale root row and an unrelated chat sit beside it.
    useChatStore.setState({
      sessions: [
        makeSession({ session_id: 'b1', thread_id: 'root' }),
        makeSession({ session_id: 'root', thread_id: 'root' }),
        makeSession({ session_id: 'other', thread_id: 'other' }),
      ],
      activeSessionId: 'b1',
      documentId: 'd1',
      models: ['m1'],
      defaultModel: 'm1',
    });

    await useChatStore.getState().deleteSession('b1');

    // A branch id would be refused 400 — the thread is deleted by its root.
    expect(apiClient.delete).toHaveBeenCalledWith('/chat/sessions/root');
    expect(apiClient.delete).not.toHaveBeenCalledWith('/chat/sessions/b1');
    expect(useChatStore.getState().sessions.map(s => s.session_id)).toEqual(['other']);
    expect(useChatStore.getState().activeSessionId).toBeNull();
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
});

describe('sendMessage — message building', () => {

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
    });
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
});

describe('agent-mode fork/regenerate gate (ST1)', () => {

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
      if (url.includes('/messages')) { return Promise.resolve({ messages: [], queue: [], queue_seq: null }); }
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

describe('loadSessions scope-change spinner (no stale flash on project switch)', () => {

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
      if (url.includes('/messages')) return Promise.resolve({ messages: [], queue: [], queue_seq: null });
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

