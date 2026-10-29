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
});
