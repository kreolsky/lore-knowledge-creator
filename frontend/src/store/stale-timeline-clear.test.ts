/**
 * Session-switch sites must clear the published timeline
 * (SYSTEM: dsh-conversation store fields: conversation/turnRanges/turnStartSeq).
 *
 * Repro (reported flash): exit a chat to the list (startGhostChat), send a new
 * message — the ghost materializes (insertAndPinSession via createSession) and
 * beginTurn writes turnStartSeq=-Infinity while the STORE still holds the previous
 * chat's published nodes; MessageList then owns every stale node to the NEW
 * streaming message (anchorSeq > -Infinity) until the first dsh publication
 * replaces the list → the previous chat's timeline flashes inside the new chat.
 *
 * INVARIANT under test: the published timeline belongs to the active session
 * — any activeSessionId transition republishes it empty (the ownership watcher
 * in chat-store.ts), EXCEPT a site that republishes a window on the same
 * transition: the piggyback restore activates first and publishes its window
 * in the same tick (load-actions.ts), so its conversation survives — pinned
 * by the last case below.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    stream: vi.fn(),
  },
}));

vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(() => Promise.resolve({})),
  fetchReferenceLinksFresh: vi.fn(() => Promise.resolve({})),
  linkCache: new Map<string, unknown>(),
}));

vi.mock('../events', () => ({
  emit: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}));

vi.mock('./app-store', () => ({
  useAppStore: { getState: vi.fn(), subscribe: () => () => {} },
}));

// Partial mock: real ui-store exports stay live (new exports used by prod code
// cannot break this mock) — only the store instance is replaced; getState is
// shaped per-test below.
vi.mock('./ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ui-store')>();
  return {
    ...actual,
    useUIStore: { getState: vi.fn() },
  };
});

vi.mock('../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  GHOST_SESSION_ID: '__ghost__',
  resolveCompletionContext: vi.fn(() => ({ context_ids: [] })),
  getContextForSession: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  setContextForSession: vi.fn(),
  clearContextForSession: vi.fn(),
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  getDerivedGhostContext: vi.fn(() => ({ docIds: [], refIds: [] })),
  ghostBaseTargets: vi.fn(() => ({ docs: [], refs: [] })),
  addItemToContext: vi.fn(() => Promise.resolve()),
  resetGhostDeltas: vi.fn(),
}));

import { useChatStore } from './chat-store';
import { apiClient } from '../api/client';
import type { ChatSession, Reference } from '../types';

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id: 'old-chat',
    project_id: 'proj-1',
    document_id: 'doc-1',
    reference_id: null,
    user_id: 'u1',
    title: '',
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as ChatSession;
}

function makeRef(): Reference {
  return { reference_id: 'ref-1', document_id: 'doc-1', title: 'r' } as unknown as Reference;
}

/**
 * Seed the store as it looks right after a turn ENDED in the previous chat: the
 * published timeline holds that chat's nodes, bound to its assistant message.
 * TWO commits: the session pointer first, the timeline second — a timeline
 * publish never moves activeSessionId in production, and one combined commit
 * would make the ownership watcher treat the seed itself as a session switch
 * (clearing the timeline and hollowing out every case below).
 */
function seedStaleTimeline() {
  useChatStore.setState({
    documentId: 'doc-1',
    activeSessionId: 'old-chat',
    sessions: [makeSession()],
    messages: [],
  });
  useChatStore.setState({
    conversation: [
      { key: 'n1', kind: 'assistant/message', anchorSeq: 3, data: { text: 'old' } },
      { key: 'n2', kind: 'reasoning', anchorSeq: 5, data: { text: 'old-thought' } },
    ],
    turnRanges: { 'old-assistant': { min: 3, max: 5 } },
    turnStartSeq: null,
  });
}

function expectTimelineCleared() {
  const s = useChatStore.getState();
  expect(s.conversation).toEqual([]);
  expect(s.turnRanges).toEqual({});
  expect(s.turnStartSeq).toBeNull();
}

beforeEach(async () => {
  vi.clearAllMocks();
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.delete as ReturnType<typeof vi.fn>).mockReset();

  const { useAppStore } = await import('./app-store');
  (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    currentProject: { project_id: 'proj-1' },
    currentDocument: { document_id: 'doc-1' },
    currentReference: null,
    currentUser: { user_id: 'u1', name: 'U' },
    accessLevel: 'full',
    showToast: vi.fn(),
    hydrateReference: vi.fn(),
  });

  const { useUIStore } = await import('./ui-store');
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => null),
    getRefPreviewMode: vi.fn(() => false),
    getRefOpenMode: vi.fn(() => 'center'),
    setRightPanelTab: vi.fn(),
  });

  window.localStorage.clear();
  useChatStore.getState().reset();
});

describe('session-switch sites clear the published timeline', () => {
  it('startGhostChat (the "← Chats" exit) clears the previous chat\'s timeline', () => {
    seedStaleTimeline();
    useChatStore.getState().startGhostChat();
    expect(useChatStore.getState().activeSessionId).toBeNull();
    expectTimelineCleared();
  });

  it('deleting the ACTIVE chat lands on a ghost with a cleared timeline', async () => {
    seedStaleTimeline();
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue({} as unknown);
    await useChatStore.getState().deleteSession('old-chat');
    expect(useChatStore.getState().activeSessionId).toBeNull();
    expectTimelineCleared();
  });

  it('openChatWithReference lands on a ghost with a cleared timeline', async () => {
    seedStaleTimeline();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      sessions: [makeSession({ session_id: 'other' })],
      active_messages: null,
    });
    await useChatStore.getState().openChatWithReference(makeRef());
    expect(useChatStore.getState().activeSessionId).toBeNull();
    expectTimelineCleared();
  });

  it('createSession materialization (first send from a ghost) clears the timeline', async () => {
    seedStaleTimeline();
    useChatStore.setState({ activeSessionId: null, turnStartSeq: null });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'new-chat' }),
    );
    const created = await useChatStore.getState().createSession({
      projectId: 'proj-1',
      documentId: 'doc-1',
    });
    expect(created?.session_id).toBe('new-chat');
    expect(useChatStore.getState().activeSessionId).toBe('new-chat');
    expectTimelineCleared();
  });

  it('loadSessions resolving to a ghost (none branch) clears the timeline', async () => {
    seedStaleTimeline();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      sessions: [],
      active_messages: null,
    });
    await useChatStore.getState().loadSessions('proj-1', 'doc-1');
    expect(useChatStore.getState().activeSessionId).toBeNull();
    expectTimelineCleared();
  });

  it('piggyback restore republishes its window AFTER activating (the watcher must not wipe it)', async () => {
    seedStaleTimeline();
    // The project's last-active chat is a session OTHER than the stale active
    // one → the resolver takes the restore_saved branch, and the piggyback
    // carries that very session's rows (with assembler frames).
    const { useUIStore } = await import('./ui-store');
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => 'saved-chat'),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      setRightPanelTab: vi.fn(),
    });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      sessions: [makeSession({ session_id: 'saved-chat' })],
      active_messages: {
        session_id: 'saved-chat',
        messages: [
          {
            message_id: 'a1',
            frames: [
              { type: 'dsh_event', kind: 'step/start', seq: 5, time: 1005, data: { turn: 0, step: 0 } },
              {
                type: 'dsh_event', kind: 'assistant/message', seq: 6, time: 1006, surfaceOp: 'append',
                data: { turn: 0, step: 0, message: { id: 'm6', role: 'assistant', content: [{ type: 'text', text: 'Hi' }], source: { kind: 'model', provider: 'lore', model: 'test' } }, stream: [] },
              },
            ],
          },
        ],
      },
    });
    await useChatStore.getState().loadSessions('proj-1', 'doc-1');
    const s = useChatStore.getState();
    expect(s.activeSessionId).toBe('saved-chat');
    // The activate-first ordering clears the OLD timeline via the watcher and
    // then republishes this window in the same tick — non-empty is the pin
    // against a future "simplification" back to publish-then-activate (which
    // the unconditional watcher would wipe to []).
    expect(s.conversation.length).toBeGreaterThan(0);
    expect(s.conversation.some(n => n.kind === 'assistant-step')).toBe(true);
    expect(s.turnRanges.a1).toEqual({ min: 5, max: 6 });
  });
});
