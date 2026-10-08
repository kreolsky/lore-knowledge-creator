/** The sessions PIGGYBACK restore must ADOPT the open turn.
 *
 * A reload mid-turn re-enters the app through loadSessions → the resolver's
 * restore_saved branch → when the backend piggybacked that session's rows
 * (with_active_messages), setActiveSession(sessionId, rows) returns early and
 * loadMessages NEVER runs. Unless that branch runs the same load steps
 * (session-rows.ts), the open turn renders as a SETTLED partial row and every
 * live chat_frame drops (no harnessTurns registration): the reload "loses" a
 * running turn — chat looks finished, nothing updates until a second reload.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    stream: vi.fn(),
  },
}));

vi.mock('../../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(() => Promise.resolve({})),
  fetchReferenceLinksFresh: vi.fn(() => Promise.resolve({})),
  linkCache: new Map<string, unknown>(),
}));

vi.mock('../../events', () => ({
  emit: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}));

vi.mock('../app-store', () => ({
  useAppStore: { getState: vi.fn(), subscribe: () => () => {} },
}));

vi.mock('../ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../ui-store')>();
  return {
    ...actual,
    useUIStore: { getState: vi.fn() },
  };
});

vi.mock('../../chat/context', () => ({
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

import { useChatStore } from '../chat-store';
import { apiClient } from '../../api/client';
import { dispatchChatFrame } from './streaming';
import { __flushFeedPublishForTest } from './conversation-feed';
import { useAppStore } from '../app-store';
import { useUIStore } from '../ui-store';
import type { ChatSession } from '../../types';

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id: 'saved-chat',
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

const chunk = (seq: number) =>
  ({
    type: 'dsh_event', kind: 'assistant/message', seq, surfaceOp: 'append',
    data: { turn: 1, step: 1, message: { id: `m${seq}`, role: 'assistant', content: [{ type: 'text', text: 'o' }], source: { kind: 'model', provider: 'lore', model: 'test' } }, stream: [] },
  });

/** The reload's piggyback rows for a mid-turn session: one OPEN assistant row
 * (the same shape GET /messages marks — the piggyback runs the same pipeline,
 * sessions_list._piggyback_messages → _attach_timeline). */
function openPiggybackRows() {
  return [{
    message_id: 'a1', chat_id: 'saved-chat', parent_id: 'u1', role: 'assistant' as const,
    content: 'partial', created_at: '2026-01-01', open_turn: true,
    frames: [chunk(6)],
  }];
}

beforeEach(async () => {
  vi.clearAllMocks();
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.delete as ReturnType<typeof vi.fn>).mockReset();

  (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    currentProject: { project_id: 'proj-1' },
    currentDocument: { document_id: 'doc-1' },
    currentReference: null,
    currentUser: { user_id: 'u1', name: 'U' },
    accessLevel: 'full',
    showToast: vi.fn(),
    hydrateReference: vi.fn(),
  });
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => 'saved-chat'),
    getRefPreviewMode: vi.fn(() => false),
    getRefOpenMode: vi.fn(() => 'center'),
    setRightPanelTab: vi.fn(),
  });

  window.localStorage.clear();
  useChatStore.getState().reset();
  (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue({
    sessions: [makeSession()],
    active_messages: { session_id: 'saved-chat', messages: openPiggybackRows() },
  });
});

describe('the piggyback restore adopts the open turn', () => {
  it('a reload mid-turn seats the streaming slot at the open row', async () => {
    await useChatStore.getState().loadSessions('proj-1', 'doc-1');

    expect(useChatStore.getState().activeSessionId).toBe('saved-chat');
    // THE regression: the piggyback branch skipped the loadMessages adoption.
    expect(useChatStore.getState().streaming?.messageId).toBe('a1');
    expect(useChatStore.getState().turnStartSeq).toBe(6);
  });

  it('the next live frames continue the adopted turn to its terminal', async () => {
    await useChatStore.getState().loadSessions('proj-1', 'doc-1');
    __flushFeedPublishForTest(useChatStore.setState);

    dispatchChatFrame(useChatStore.getState, useChatStore.setState, 'saved-chat', chunk(7));
    __flushFeedPublishForTest(useChatStore.setState);
    dispatchChatFrame(useChatStore.getState, useChatStore.setState, 'saved-chat',
      { type: 'done', content: 'Hello world' });
    dispatchChatFrame(useChatStore.getState, useChatStore.setState, 'saved-chat',
      { type: 'turn_closed' });

    expect(useChatStore.getState().streaming).toBeNull();
    expect(useChatStore.getState().turnRanges['a1']).toEqual({ min: 6, max: 7 });
    expect(useChatStore.getState().messages.find(m => m.message_id === 'a1')?.content)
      .toBe('Hello world');
  });
});
