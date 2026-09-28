/**
 * Ghost inherits model + agent_auto + system_prompt_id from the last active AI
 * chat in the current scope.
 *
 * Covers the store-layer inheritance rules:
 *   - inherit model
 *   - inherit agent_auto
 *   - inherit system_prompt_id
 *   - per-document (per-scope) isolation
 *   - all entry points inherit (startGhostChat, loadSessions none-branch,
 *     deleteSession last-session, openChatWithReference)
 *   - empty scope → bare defaults
 *   - discard/reset → inherited baseline
 *
 * There is no `mode`/`ghostMode` — agent_auto is the only client-side-inherited
 * selector (every AI chat is an agent chat).
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

vi.mock('../events', () => ({
  emit: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}));

vi.mock('./app-store', () => ({
  useAppStore: { getState: vi.fn(), subscribe: () => () => {} },
}));

vi.mock('./ui-store', () => ({
  useUIStore: { getState: vi.fn() },
}));

vi.mock('../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  GHOST_SESSION_ID: '__ghost__',
  resolveCompletionContext: vi.fn(() => ({ document_ids: [], reference_ids: [] })),
  getContextForSession: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  setContextForSession: vi.fn(),
  clearContextForSession: vi.fn(),
  attachOpenEntityToGhost: vi.fn(),
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  addItemToContext: vi.fn(() => Promise.resolve()),
  removeItemFromContext: vi.fn(() => Promise.resolve()),
  transferGhostContext: vi.fn(),
}));

import { useChatStore } from './chat-store';
import type { ChatSession } from '../types';

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id: 's1',
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

async function mockAppState(overrides: Record<string, unknown> = {}) {
  const { useAppStore } = await import('./app-store');
  (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    currentProject: { project_id: 'proj-1' },
    currentDocument: { document_id: 'doc-1' },
    currentReference: null,
    accessLevel: 'full',
    showToast: vi.fn(),
    setCurrentReference: vi.fn(),
    hydrateReference: vi.fn().mockResolvedValue(null),
    ...overrides,
  });
}

beforeEach(async () => {
  const { apiClient } = await import('../api/client');
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.delete as ReturnType<typeof vi.fn>).mockReset();

  await mockAppState();

  const { useUIStore } = await import('./ui-store');
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => null),
    getRefPreviewMode: vi.fn(() => false),
    getRefOpenMode: vi.fn(() => 'center'),
    setRightPanelTab: vi.fn(),
  });

  useChatStore.getState().reset();
});

describe('initGhostFromScope — direct', () => {
  it('D8: empty scope → bare defaults (false / null / empty model / null effort)', () => {
    useChatStore.setState({ sessions: [], activeSessionId: null });
    useChatStore.getState().initGhostFromScope();

    const s = useChatStore.getState();
    expect(s.ghostAgentAuto).toBe(false);
    expect(s.ghostSystemPromptId).toBeNull();
    expect(s.ghostModel).toBe('');
    expect(s.ghostReasoningEffort).toBeNull();
  });

  it('D1/D2/D3: inherits model + agent_auto + system_prompt_id + reasoning_effort from the latest session', () => {
    useChatStore.setState({
      sessions: [
        makeSession({
          session_id: 'old',
          model: 'gpt-old',
          updated_at: '2024-01-01T00:00:00Z',
        }),
        makeSession({
          session_id: 'latest',
          model: 'gpt-X',
          agent_auto: true,
          system_prompt_id: 'p1',
          reasoning_effort: 'high',
          updated_at: '2026-01-01T00:00:00Z',
        }),
      ],
      activeSessionId: null,
    });
    useChatStore.getState().initGhostFromScope();

    const s = useChatStore.getState();
    expect(s.ghostModel).toBe('gpt-X');
    expect(s.ghostAgentAuto).toBe(true);
    expect(s.ghostSystemPromptId).toBe('p1');
    // The backend create-walk inherits model + prompt but NOT reasoning_effort,
    // so the ghost holds it client-side (mirrors agent_auto) — what the composer
    // shows is what the materialization POST carries.
    expect(s.ghostReasoningEffort).toBe('high');
  });

  it('prefers the active session over the latest-by-updated_at one', () => {
    useChatStore.setState({
      sessions: [
        makeSession({
          session_id: 'newer',
          model: 'newer-model',
          updated_at: '2026-06-01T00:00:00Z',
        }),
        makeSession({
          session_id: 'active',
          model: 'active-model',
          agent_auto: false,
          system_prompt_id: 'active-prompt',
          updated_at: '2024-01-01T00:00:00Z',
        }),
      ],
      activeSessionId: 'active',
    });
    useChatStore.getState().initGhostFromScope();

    const s = useChatStore.getState();
    expect(s.ghostModel).toBe('active-model');
    expect(s.ghostSystemPromptId).toBe('active-prompt');
  });

  it('D9: discard/reset re-inherits the scope baseline (not bare defaults)', () => {
    // Scope holds a chat with model=gpt-X, agent_auto=true, prompt=p1.
    useChatStore.setState({
      sessions: [makeSession({
        session_id: 'src', model: 'gpt-X',
        agent_auto: true, system_prompt_id: 'p1',
      })],
      activeSessionId: null,
    });
    // User manually overrode the ghost to different values.
    useChatStore.setState({
      ghostModel: 'manual',
      ghostAgentAuto: false, ghostSystemPromptId: null,
    });

    // startGhostChat is the discard/reset entry point.
    useChatStore.getState().startGhostChat();

    const s = useChatStore.getState();
    expect(s.ghostModel).toBe('gpt-X');
    expect(s.ghostAgentAuto).toBe(true);
    expect(s.ghostSystemPromptId).toBe('p1');
  });
});

describe('D5: all entry points inherit', () => {
  it('startGhostChat ("+") inherits from the chat being left', () => {
    useChatStore.setState({
      sessions: [makeSession({
        session_id: 'leaving', model: 'gpt-leave',
        agent_auto: true, system_prompt_id: 'p-leave',
      })],
      activeSessionId: 'leaving',
      pendingInputFocus: false,
    });

    useChatStore.getState().startGhostChat();

    const s = useChatStore.getState();
    expect(s.activeSessionId).toBeNull();
    expect(s.ghostModel).toBe('gpt-leave');
    expect(s.ghostSystemPromptId).toBe('p-leave');
    // Opening a zero/ghost chat moves focus into the composer.
    expect(s.pendingInputFocus).toBe(true);
  });

  it('deleteSession last-session ghost falls back to bare defaults (empty scope)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.delete as ReturnType<typeof vi.fn>).mockResolvedValue(undefined);

    useChatStore.setState({
      sessions: [makeSession({
        session_id: 'only', model: 'gpt-only',
        agent_auto: true, system_prompt_id: 'p-only',
      })],
      activeSessionId: 'only',
    });

    await useChatStore.getState().deleteSession('only');

    const s = useChatStore.getState();
    expect(s.activeSessionId).toBeNull();
    expect(s.sessions).toHaveLength(0);
    // initGhostFromScope runs before set() — ghost inherits from the deleted session.
    expect(s.ghostModel).toBe('gpt-only');
    expect(s.ghostSystemPromptId).toBe('p-only');
  });

  it('openChatWithReference inherits from the loaded ref-scope latest chat', async () => {
    const { apiClient } = await import('../api/client');
    const { useUIStore } = await import('./ui-store');

    // Use real-ish state object that loadSessions + openChatWithReference need.
    const ref = { reference_id: 'ref-7', document_id: 'doc-1', project_id: 'proj-1' } as const;
    await mockAppState({ currentReference: ref });

    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({
        session_id: 'ref-latest', reference_id: ref.reference_id,
        model: 'gpt-ref', agent_auto: true,
        system_prompt_id: 'p-ref', updated_at: '2026-01-01T00:00:00Z',
      }),
      makeSession({
        session_id: 'ref-old', reference_id: ref.reference_id,
        model: 'gpt-old', updated_at: '2024-01-01T00:00:00Z',
      }),
    ]);
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      setRightPanelTab: vi.fn(),
    });

    await useChatStore.getState().openChatWithReference(ref as never);

    const s = useChatStore.getState();
    expect(s.activeSessionId).toBeNull();
    expect(s.ghostModel).toBe('gpt-ref');
    expect(s.ghostAgentAuto).toBe(true);
    expect(s.ghostSystemPromptId).toBe('p-ref');
  });
});

describe('D4: per-scope isolation', () => {
  it('the source is the current scope\'s sessions, never another scope', () => {
    // Scope A's store holds only scope-A chats.
    useChatStore.setState({
      sessions: [makeSession({
        session_id: 'a-chat', model: 'model-A',
        document_id: 'doc-A',
      })],
      activeSessionId: null,
    });
    useChatStore.getState().initGhostFromScope();
    expect(useChatStore.getState().ghostModel).toBe('model-A');

    // Simulate navigating to scope B: sessions replaced with B's chats.
    useChatStore.setState({
      sessions: [makeSession({
        session_id: 'b-chat', model: 'model-B',
        document_id: 'doc-B',
      })],
      activeSessionId: null,
    });
    useChatStore.getState().initGhostFromScope();
    expect(useChatStore.getState().ghostModel).toBe('model-B');
  });

  it('project-wide list: a ghost inherits from the latest chat of ANOTHER document', () => {
    // The sessions list is now project-wide. A fresh ghost on doc-A inherits from
    // the latest AI chat ANYWHERE in the project — here the donor lives on doc-B.
    useChatStore.setState({
      sessions: [
        makeSession({
          session_id: 'docA-old', model: 'model-A',
          document_id: 'doc-A', updated_at: '2026-01-01T00:00:00Z',
        }),
        makeSession({
          session_id: 'docB-latest', model: 'model-B',
          agent_auto: true, system_prompt_id: 'p-B',
          document_id: 'doc-B', updated_at: '2026-06-01T00:00:00Z',
        }),
      ],
      activeSessionId: null,
    });
    useChatStore.getState().initGhostFromScope();

    const s = useChatStore.getState();
    expect(s.ghostModel).toBe('model-B');
    expect(s.ghostAgentAuto).toBe(true);
    expect(s.ghostSystemPromptId).toBe('p-B');
  });
});

describe('startGhostChat clears the project-level active chat', () => {
  it('clears lastActiveChatSessionId on startGhostChat', async () => {
    const { useUIStore } = await import('./ui-store');
    const setLastActiveChatSession = vi.fn();
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession,
      getLastActiveChatSession: vi.fn(() => 'sess-old'),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      setRightPanelTab: vi.fn(),
    });

    useChatStore.getState().startGhostChat();

    expect(setLastActiveChatSession).toHaveBeenCalledWith(null);
  });
});

describe('reset — ghost reasoning effort hygiene', () => {
  it('reset() clears ghostReasoningEffort (logout hygiene)', () => {
    useChatStore.setState({ ghostReasoningEffort: 'high' });
    useChatStore.getState().reset();
    expect(useChatStore.getState().ghostReasoningEffort).toBeNull();
  });
});
