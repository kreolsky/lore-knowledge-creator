/**
 * Integration: ChatInput lazy-create → createSession snapshots the DERIVED ghost
 * context.
 *
 * createSession no longer cascades first-circle at create time. It synchronously
 * snapshots getDerivedGhostContext() — which reads the warm linkCache + ghost
 * deltas — onto the real id via setContextForSession. These tests use the REAL
 * context.ts (bridge wired to the mocked app/ui stores) to verify the snapshot is
 * what resolveCompletionContext sends on the first turn.
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

// Partial mock: real ui-store exports stay live (the split test below relies on
// the REAL showsBothPanes classifying 'split' vs 'center') — only the store
// instance is replaced; getState is shaped per-test via mockUIState().
vi.mock('./ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./ui-store')>();
  return {
    ...actual,
    useUIStore: { getState: vi.fn() },
  };
});

// REAL linkCache Map so getDerivedGhostContext (which reads it directly) works; the
// warm-effect's *Fresh fetchers are stubbed (not exercised in a store test).
vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(),
  fetchReferenceLinksFresh: vi.fn(),
  linkCache: new Map(),
}));

import { useChatStore } from './chat-store';
import {
  getContextForSession,
  clearContextForSession,
  removeGhostDelta,
  __resetGhostDeltaStore,
  GHOST_SESSION_ID,
} from '../chat/context';
import { linkCache, fetchDocumentLinksFresh, fetchReferenceLinksFresh } from '../api/links';
import type { FirstCircle } from '../utils/cascade-selection';
import type { ChatSession } from '../types';

// linkCache is exported as ReadonlyMap; the mock backs it with a real Map, so cast
// to populate it — the derive selector reads the same instance via .get.
const cache = linkCache as unknown as Map<string, FirstCircle>;

const EMPTY_FIRST_CIRCLE: FirstCircle = { document_ids: [], reference_ids: [] };

// Mirror production: the real *Fresh fetchers FILL the shared linkCache, and the
// createSession derive pass reads the cache. These impls write into `cache` so a
// warm-before-snapshot derive sees the same first-circle the picker would show.
function primeDocLinks(links: FirstCircle): void {
  vi.mocked(fetchDocumentLinksFresh).mockImplementation((id: string) => {
    cache.set(`doc:${id}`, links);
    return Promise.resolve(links);
  });
}

function primeRefLinks(links: FirstCircle): void {
  vi.mocked(fetchReferenceLinksFresh).mockImplementation((id: string) => {
    cache.set(`ref:${id}`, links);
    return Promise.resolve(links);
  });
}

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
    mode: 'ask',
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
    references: [],
    ...overrides,
  });
}

async function mockUIState(overrides: Record<string, unknown> = {}) {
  const { useUIStore } = await import('./ui-store');
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => null),
    getRefPreviewMode: vi.fn(() => false),
    getRefOpenMode: vi.fn(() => 'center'),
    setRightPanelTab: vi.fn(),
    ...overrides,
  });
}

beforeEach(async () => {
  const { apiClient } = await import('../api/client');
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.patch as ReturnType<typeof vi.fn>).mockReset();

  await mockAppState();
  await mockUIState();

  cache.clear();
  __resetGhostDeltaStore();
  clearContextForSession('s1');
  clearContextForSession(GHOST_SESSION_ID);

  // createSession warms the base first-circle into the shared linkCache before the
  // derive pass. Default both fetchers to an empty first-circle (still filling the
  // cache, like production) so a test that doesn't override stays at the bare base id.
  const { fetchDocumentLinksFresh, fetchReferenceLinksFresh } = await import('../api/links');
  vi.mocked(fetchDocumentLinksFresh).mockReset();
  vi.mocked(fetchReferenceLinksFresh).mockReset();
  primeDocLinks(EMPTY_FIRST_CIRCLE);
  primeRefLinks(EMPTY_FIRST_CIRCLE);

  useChatStore.getState().reset();
});

describe('createSession — derived snapshot (real context.ts)', () => {

  // Regression (the reported bug): in the zero/ghost chat the user unchecks a
  // first-circle child of the open doc, then sends. The snapshot must keep that
  // removal — the old post-snapshot backfill force-re-added the whole first-circle
  // and clobbered the manual removal. The fix warms the cache BEFORE the single
  // derive pass, so subtractWithCascade runs last and the child stays removed.
  it('removed first-circle child survives materialization (no reset to default)', async () => {
    const { apiClient } = await import('../api/client');
    primeDocLinks({ document_ids: ['linked-doc'], reference_ids: [] });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'rm-1' }));

    removeGhostDelta('doc', 'linked-doc'); // user unchecks the first-circle child
    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const ctx = getContextForSession('rm-1');
    expect(ctx.documentIds).toEqual(['doc-1']); // linked-doc NOT re-added
    expect(ctx.documentIds).not.toContain('linked-doc');
  });
});
