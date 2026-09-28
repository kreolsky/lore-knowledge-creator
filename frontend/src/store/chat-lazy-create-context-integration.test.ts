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
  resolveCompletionContext,
  getContextForSession,
  clearContextForSession,
  addGhostDelta,
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
  it('cold cache: snapshot is the bare open doc (no first-circle yet)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'cold-1' }));

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    expect(getContextForSession('cold-1')).toEqual({ documentIds: ['doc-1'], referenceIds: [] });
  });

  // Characterization: an open REFERENCE alone
  // (non-split, no open doc) lands in the derived snapshot via currentReference — the
  // derive base is UNCONDITIONAL on the open entity and needs no talkTo flag. This
  // is the real-path proof that removing the write-only talkTo state is safe: a
  // "Chat with Reference" flow (hydrateReference → currentReference) materializes a
  // ref-scoped snapshot with the ref in context. PASSES before and after talkTo removal.
  it('ref-only base: snapshot includes the open reference (no talkTo flag needed)', async () => {
    const { apiClient } = await import('../api/client');
    await mockAppState({ currentDocument: null, currentReference: { reference_id: 'ref-open' } });
    await mockUIState({ getRefOpenMode: () => 'center' });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'ref-only-1', reference_id: 'ref-open' }),
    );

    await useChatStore.getState().createSession({ projectId: 'proj-1' });

    expect(getContextForSession('ref-only-1')).toEqual({ documentIds: [], referenceIds: ['ref-open'] });
  });

  // Regression (review finding: cold linkCache at materialization). The warm-effect
  // may not have run (e.g. region-agent from the editor). The snapshot alone would be
  // bare-id-only and stored permanently. The createSession first-circle BACKFILL must
  // still guarantee the open entity + its first-circle on turn 1 regardless of cache
  // warmth — no silent display-vs-send disagreement.
  it('cold cache + fresh fetcher: backfill guarantees first-circle on the materialized session', async () => {
    const { apiClient } = await import('../api/client');
    // Cache stays COLD; the fresh fetcher returns first-circle (the warm path).
    primeDocLinks({ document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'backfill-1' }));

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const ctx = getContextForSession('backfill-1');
    // The open doc + its first-circle are present even though linkCache was cold.
    expect(ctx.documentIds.sort()).toEqual(['doc-1', 'linked-doc'].sort());
    expect(ctx.referenceIds).toContain('linked-ref');
  });

  it('warm cache: snapshot includes first-circle from linkCache', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'warm-1' }));
    cache.set('doc:doc-1', { document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const ctx = getContextForSession('warm-1');
    expect(ctx.documentIds.sort()).toEqual(['doc-1', 'linked-doc'].sort());
    expect(ctx.referenceIds).toEqual(['linked-ref']);
  });

  it('send/display agree: resolveCompletionContext returns the snapshot', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'agree-1' }));
    cache.set('doc:doc-1', { document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    // activeSessionId is 'agree-1' → resolveCompletionContext reads its STORED snapshot.
    expect(resolveCompletionContext().context_ids.sort()).toEqual(
      ['doc-1', 'linked-doc', 'linked-ref'].sort(),
    );
  });

  it('ghost manual delta survives materialization (snapshot folds deltas)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'pick-1' }));

    addGhostDelta('doc', 'picked-doc');
    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const ctx = getContextForSession('pick-1');
    expect(ctx.documentIds.sort()).toEqual(['doc-1', 'picked-doc'].sort());
  });

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

  it('split view: snapshot has both panes + both first-circles', async () => {
    const { apiClient } = await import('../api/client');
    await mockAppState({
      currentReference: { reference_id: 'ref-split' },
      currentDocument: { document_id: 'doc-1' },
    });
    await mockUIState({ getRefOpenMode: (id: string) => (id === 'doc-1' ? 'split' : 'center') });
    cache.set('doc:doc-1', { document_ids: ['doc-linked'], reference_ids: ['ref-from-doc'] });
    cache.set('ref:ref-split', { document_ids: ['doc-from-ref'], reference_ids: ['ref-linked'] });
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(makeSession({ session_id: 'split-1' }));

    await useChatStore.getState().createSession({ projectId: 'proj-1', documentId: 'doc-1' });

    const ctx = getContextForSession('split-1');
    expect(ctx.documentIds.sort()).toEqual(['doc-1', 'doc-from-ref', 'doc-linked'].sort());
    expect(ctx.referenceIds.sort()).toEqual(['ref-split', 'ref-from-doc', 'ref-linked'].sort());
  });

  it('ghost delta writes never PATCH the non-existent __ghost__ session', async () => {
    const showToast = vi.fn();
    await mockAppState({ showToast });

    expect(useChatStore.getState().activeSessionId).toBeNull();
    addGhostDelta('doc', 'ghost-pick');
    await new Promise(r => setTimeout(r, 300));

    const patchCalls = (await import('../api/client')).apiClient.patch as ReturnType<typeof vi.fn>;
    const ghostPatches = patchCalls.mock.calls.filter(
      (c: string[]) => typeof c[0] === 'string' && c[0].includes('__ghost__'),
    );
    expect(ghostPatches).toHaveLength(0);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('ghost overrides applied at materialization (agent_auto/system-prompt)', async () => {
    const { apiClient } = await import('../api/client');
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue(
      makeSession({ session_id: 'ghost-ovr', target_doc_id: 'doc-1' }),
    );

    useChatStore.setState({ ghostAgentAuto: true, ghostSystemPromptId: 'prompt-x' });
    const ghost = useChatStore.getState();
    await useChatStore.getState().createSession({
      projectId: 'proj-1', documentId: 'doc-1', systemPromptId: ghost.ghostSystemPromptId,
      agentAuto: ghost.ghostAgentAuto, targetDocId: 'doc-1',
    });

    expect(apiClient.post).toHaveBeenCalledWith('/chat/sessions', expect.objectContaining({
      agent_auto: true,
      system_prompt_id: 'prompt-x',
      target_doc_id: 'doc-1',
    }));
  });
});
