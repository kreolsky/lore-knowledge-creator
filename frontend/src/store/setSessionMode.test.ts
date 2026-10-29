/**
 * Apply-mode dropdown + system-prompt store behaviour. Since
 * remove-ask-line-mode-axis, the ask↔agent axis is gone:
 *   - setSessionMode(systemPromptId) PATCHes ONLY system_prompt_id in place
 *     (no mode boundary to cross — every AI chat is an agent chat).
 *   - setSessionUIMode(mode) toggles the persisted agent_auto (confirm↔auto),
 *     the surviving selector.
 *   - normalizeMode is deleted (no mode axis); deriveUIMode reads agent_auto.
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
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  addItemToContext: vi.fn(() => Promise.resolve()),
  removeItemFromContext: vi.fn(() => Promise.resolve()),
  transferGhostContext: vi.fn(),
}));

import { useChatStore } from './chat-store';
import { deriveUIMode } from '../types';
import type { ChatSession } from '../types';

function makeSession(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    session_id: 's-agent',
    project_id: 'proj-1',
    document_id: 'doc-1',
    reference_id: null,
    user_id: 'u1',
    title: '',
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    target_doc_id: 'doc-1',
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  } as ChatSession;
}

let appGetState: ReturnType<typeof vi.fn> | null = null;

async function setAppState(access: string, showToast: ReturnType<typeof vi.fn> = vi.fn(), refOverride: { reference_id: string } | null = null) {
  const { useAppStore } = await import('./app-store');
  appGetState = useAppStore.getState as ReturnType<typeof vi.fn>;
  appGetState.mockReturnValue({
    currentProject: { project_id: 'proj-1' },
    currentDocument: { document_id: 'doc-1' },
    currentReference: refOverride,
    accessLevel: access,
    showToast,
    setCurrentReference: vi.fn(),
  });
}

beforeEach(async () => {
  const { apiClient } = await import('../api/client');
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.patch as ReturnType<typeof vi.fn>).mockReset();

  await setAppState('full');

  const { useUIStore } = await import('./ui-store');
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => null),
    getRefPreviewMode: vi.fn(() => false),
    setRightPanelTab: vi.fn(),
  });

  const { addItemToContext } = await import('../chat/context');
  (addItemToContext as ReturnType<typeof vi.fn>).mockClear();

  if (typeof window.localStorage.clear === 'function') window.localStorage.clear();
  useChatStore.getState().reset();
});

// ─── deriveUIMode (reads only agent_auto) ──────────────────────────────────
describe('deriveUIMode', () => {

  it('maps null/undefined session to agent_confirm (no active session)', () => {
    expect(deriveUIMode(null)).toBe('agent_confirm');
    expect(deriveUIMode(undefined)).toBe('agent_confirm');
  });
});

// ─── setSessionUIMode (the surviving confirm↔auto toggle) ───────────────────
describe('setSessionUIMode', () => {
  beforeEach(async () => {
    const sess = makeSession({ session_id: 's1', agent_auto: false });
    useChatStore.setState({ sessions: [sess], activeSessionId: 's1' });
  });

  it('updates the session row so deriveUIMode reflects the PATCH (agent_auto survives)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.patch as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 's1', agent_auto: true }),
    );

    useChatStore.getState().setSessionUIMode('agent_auto');
    await new Promise(r => setTimeout(r, 0));

    const sess = useChatStore.getState().sessions.find(s => s.session_id === 's1');
    expect(deriveUIMode(sess)).toBe('agent_auto');
  });

  it('derived uiMode survives reload (sessions reloaded from server with agent_auto)', async () => {
    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1', agent_auto: true })],
      activeSessionId: 's1',
    });
    const sess = useChatStore.getState().sessions.find(s => s.session_id === 's1');
    expect(deriveUIMode(sess)).toBe('agent_auto');

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1', agent_auto: false })],
      activeSessionId: 's1',
    });
    const sess2 = useChatStore.getState().sessions.find(s => s.session_id === 's1');
    expect(deriveUIMode(sess2)).toBe('agent_confirm');
  });

  it('blocks agent_auto without full access (no PATCH, toast)', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const showToast = vi.fn();
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'proj-1' },
      currentDocument: { document_id: 'doc-1' },
      currentReference: null,
      accessLevel: 'readonly',
      showToast,
    });
    (apiClient.patch as ReturnType<typeof vi.fn>).mockClear();

    useChatStore.getState().setSessionUIMode('agent_auto');

    expect(apiClient.patch).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });

  it('surfaces a toast on a failed PATCH (updateSession catches)', async () => {
    const { apiClient } = await import('../api/client');
    const showToast = vi.fn();
    const { useAppStore } = await import('./app-store');
    (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      currentProject: { project_id: 'proj-1' },
      currentDocument: { document_id: 'doc-1' },
      currentReference: null,
      accessLevel: 'full',
      showToast,
    });
    (apiClient.patch as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('fail'));

    useChatStore.setState({
      sessions: [makeSession({ session_id: 's1', agent_auto: false })],
      activeSessionId: 's1',
    });

    useChatStore.getState().setSessionUIMode('agent_auto');
    await new Promise(r => setTimeout(r, 10));

    expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
  });
});
