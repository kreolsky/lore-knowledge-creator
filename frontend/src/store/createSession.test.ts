/**
 * createSession. The ghost context is now
 * DERIVED; createSession snapshots deriveGhostContext() onto the real id via
 * setContextForSession + resets the ghost deltas. Context SELECTION (open entity,
 * split, first-circle) is the pure selector's job — tested in
 * ghost-context.test.ts. These tests verify:
 *   1. POST shape: target_doc_id/agent_auto/system_prompt_id (see
 *      remove-ask-line-mode-axis — `mode` left the wire; every create is agent).
 *   2. The derived snapshot is persisted onto the materialized id (+ deltas reset).
 *   3. target_doc_id FIELD is set on the POST unconditionally (orthogonal to context).
 *   4. Failure surfacing + access guard (unchanged).
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

// Partial mock: real ui-store exports stay live (new exports used by prod code
// cannot break this mock) — only the store instance is replaced; getState is
// shaped per-test in the beforeEach below.
vi.mock('./ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ui-store')>();
  return {
    ...actual,
    useUIStore: { getState: vi.fn() },
  };
});

const DERIVED_SNAPSHOT = { docIds: ['doc-1'], refIds: [] };

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
  getDerivedGhostContext: vi.fn(() => ({ ...DERIVED_SNAPSHOT })),
  ghostBaseTargets: vi.fn((docId: string | null, refId: string | null, split: boolean) => {
    const docs: string[] = []; const refs: string[] = [];
    if (split) { if (docId) docs.push(docId); if (refId) refs.push(refId); }
    else if (refId) refs.push(refId);
    else if (docId) docs.push(docId);
    return { docs, refs };
  }),
  addItemToContext: vi.fn(() => Promise.resolve()),
  resetGhostDeltas: vi.fn(),
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
    ...overrides,
  });
}

beforeEach(async () => {
  const { apiClient } = await import('../api/client');
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.patch as ReturnType<typeof vi.fn>).mockReset();

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

  const { setContextForSession, resetGhostDeltas, getDerivedGhostContext } = await import('../chat/context');
  (setContextForSession as ReturnType<typeof vi.fn>).mockClear();
  (resetGhostDeltas as ReturnType<typeof vi.fn>).mockClear();
  (getDerivedGhostContext as ReturnType<typeof vi.fn>).mockClear();
  (getDerivedGhostContext as ReturnType<typeof vi.fn>).mockReturnValue({ ...DERIVED_SNAPSHOT });

  window.localStorage.clear();
  useChatStore.getState().reset();
});

describe('createSession — POST shape (plan: remove-ask-line-mode-axis, D3)', () => {
  it('POSTs target_doc_id + agent_auto (no mode on the wire)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'agent-1', target_doc_id: 'doc-1' }),
    );

    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', parentSessionId: null,
      targetDocId: 'doc-1',
    });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', expect.objectContaining({
      target_doc_id: 'doc-1',
      document_id: 'doc-1',
    }));
    // No `mode` key on the POST body.
    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body).not.toHaveProperty('mode');
    expect(useChatStore.getState().activeSessionId).toBe('agent-1');
  });

  it('forwards agent_auto:true at materialization (ghost override)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'ghost-auto', target_doc_id: 'doc-1' }),
    );

    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', systemPromptId: 'prompt-x', parentSessionId: null,
      agentAuto: true, targetDocId: 'doc-1',
    });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', expect.objectContaining({
      agent_auto: true,
      target_doc_id: 'doc-1',
      system_prompt_id: 'prompt-x',
    }));
  });

  it('forwards target_doc_id + agent_auto:false by default (every AI chat is agent)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'agent-default', target_doc_id: 'doc-1' }),
    );

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body).not.toHaveProperty('mode');
    expect(body.target_doc_id).toBe('doc-1');
    expect(body.agent_auto).toBe(false);
  });
});

describe('createSession — derived context snapshot', () => {
  it('snapshots deriveGhostContext() onto the real id via setContextForSession + resets deltas', async () => {
    const { apiClient } = await import('../api/client');
    const { setContextForSession, resetGhostDeltas, getDerivedGhostContext } = await import('../chat/context');
    (getDerivedGhostContext as ReturnType<typeof vi.fn>).mockReturnValue({ docIds: ['doc-1', 'linked'], refIds: ['ref-1'] });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'snap-1' }));

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    // The derived snapshot is persisted verbatim onto the materialized id.
    expect(setContextForSession).toHaveBeenCalledWith('snap-1', ['doc-1', 'linked'], ['ref-1']);
    expect(resetGhostDeltas).toHaveBeenCalled();
  });

  it('applies to every AI chat — context is NOT special-cased (Decision 2)', async () => {
    const { apiClient } = await import('../api/client');
    const { setContextForSession, getDerivedGhostContext } = await import('../chat/context');
    (getDerivedGhostContext as ReturnType<typeof vi.fn>).mockReturnValue({ docIds: ['agent-target'], refIds: [] });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'agent-snap', target_doc_id: 'agent-target' }),
    );

    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'agent-target', parentSessionId: null,
      targetDocId: 'agent-target',
    });

    // Same snapshot path — no addItemToContext, no special agent-context branch.
    expect(setContextForSession).toHaveBeenCalledWith('agent-snap', ['agent-target'], []);
  });

  it('snapshots synchronously before returning (first turn sees full context)', async () => {
    const { apiClient } = await import('../api/client');
    const { setContextForSession } = await import('../chat/context');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'sync-snap' }));

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    // setContextForSession is called synchronously within createSession (no await
    // between the POST resolve and the snapshot) — a single call, immediate.
    expect(setContextForSession).toHaveBeenCalledTimes(1);
    expect(setContextForSession).toHaveBeenCalledWith('sync-snap', ['doc-1'], []);
  });
});

describe('createSession — error surfacing', () => {
  it('shows an error toast and returns null when the POST fails (no silent degradation)', async () => {
    const { apiClient } = await import('../api/client');
    const showToast = vi.fn();
    await mockAppState({ showToast });
    (apiClient.post as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    const result = await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    expect(result).toBeNull();
    expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
    expect(useChatStore.getState().activeSessionId).toBeNull();
  });

  it('uses the agent-specific toast message on failure', async () => {
    const { apiClient } = await import('../api/client');
    const showToast = vi.fn();
    await mockAppState({ showToast });
    (apiClient.post as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('boom'));

    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', parentSessionId: null,
      targetDocId: 'doc-1',
    });

    expect(showToast).toHaveBeenCalledWith('Failed to start agent mode', 'error');
  });
});

describe('createSession — access guard', () => {
  it('blocks creation without full access (no POST, toast, returns null)', async () => {
    const { apiClient } = await import('../api/client');
    const showToast = vi.fn();
    await mockAppState({ accessLevel: 'readonly', showToast });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'should-not-create' }),
    );

    const result = await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', parentSessionId: null,
      targetDocId: 'doc-1',
    });

    expect(result).toBeNull();
    expect(apiClient.post).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('Agent mode requires full project access', 'error');
  });

  it('allows creation with full access', async () => {
    const { apiClient } = await import('../api/client');
    await mockAppState({ accessLevel: 'full' });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'agent-ok', target_doc_id: 'doc-1' }),
    );

    const result = await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    expect(result).not.toBeNull();
    expect(result?.session_id).toBe('agent-ok');
  });
});

describe('createSession — "+" preserves parent_session_id (regression)', () => {
  it('a second createSession keeps parent_session_id', async () => {
    const { apiClient } = await import('../api/client');

    const first = makeSession({ session_id: 'agent-a', target_doc_id: 'doc-1' });
    useChatStore.setState({ sessions: [first], activeSessionId: 'agent-a' });

    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'agent-b', target_doc_id: 'doc-1' }),
    );

    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', parentSessionId: 'agent-a',
      targetDocId: 'doc-1',
    });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', expect.objectContaining({
      target_doc_id: 'doc-1', parent_session_id: 'agent-a',
    }));
    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body).not.toHaveProperty('mode');
    expect(useChatStore.getState().activeSessionId).toBe('agent-b');
  });
});

describe('createSession — system_prompt_id null vs undefined', () => {
  it('sends system_prompt_id: null when passed null (explicit Default)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'null-spid' }),
    );

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1', systemPromptId: null });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', expect.objectContaining({
      system_prompt_id: null,
    }));
  });

  it('omits system_prompt_id when passed undefined (caller wants inheritance)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'inherit-spid' }),
    );

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const body = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(body).not.toHaveProperty('system_prompt_id');
  });
});
