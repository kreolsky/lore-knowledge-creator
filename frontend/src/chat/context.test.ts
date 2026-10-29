/** Unit tests for chat context module — debounce, rollback, prune, resolveCompletionContext, clear with cancel. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: {
    patch: vi.fn(() => Promise.resolve({})),
  },
}));

vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(),
  fetchReferenceLinksFresh: vi.fn(),
}));

import { apiClient } from '../api/client';
import {
  setupChatContextBridge,
  resetChatContextBridge,
  getContextForSession,
  setContextForSession,
  clearContextForSession,
  hydrateFromSessions,
  pruneContext,
  resolveCompletionContext,
} from './context';

function setupBridge(overrides: Partial<Parameters<typeof setupChatContextBridge>[0]> & { getSplitView?: (docId: string) => boolean } = {}) {
  const { getSplitView: _getSplitView, ...bridgeOverrides } = overrides;
  setupChatContextBridge({
    updateChatSession: vi.fn(),
    showToast: vi.fn(),
    getActiveSessionId: () => 's1',
    getAppStoreState: () => ({ currentDocument: null, currentReference: null }),
    getSplitView: _getSplitView ?? (() => false),
    // Sentinel ref id keeps the refIdSet non-empty so the Item-4 observability
    // warning doesn't fire in tests that don't exercise it. The sentinel never
    // matches a real test id, so doc/ref classification is unaffected. Tests that
    // assert the warning override getReferenceIdSet explicitly.
    getReferenceIdSet: () => new Set(['__test_no_warn_sentinel__']),
    ...bridgeOverrides,
  } as Parameters<typeof setupChatContextBridge>[0]);
}

function waitDebounce() {
  vi.advanceTimersByTime(250);
}

describe('setContextForSession', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(apiClient.patch).mockClear();
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 's1' });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('updates Zustand immediately and debounces PATCH by 200ms', () => {
    setupBridge();

    setContextForSession('s1', ['doc1'], ['ref1']);
    expect(getContextForSession('s1')).toEqual({ documentIds: ['doc1'], referenceIds: ['ref1'] });
    expect(apiClient.patch).not.toHaveBeenCalled();

    vi.advanceTimersByTime(150);
    expect(apiClient.patch).not.toHaveBeenCalled();

    vi.advanceTimersByTime(100);
    expect(apiClient.patch).toHaveBeenCalledTimes(1);
    expect(apiClient.patch).toHaveBeenCalledWith('/chat/sessions/s1', {
      context_ids: ['doc1', 'ref1'],
    });
  });

  it('coalesces rapid calls into a single PATCH', () => {
    setupBridge();

    setContextForSession('s1', ['doc1'], ['ref1']);
    setContextForSession('s1', ['doc1', 'doc2'], ['ref1']);
    setContextForSession('s1', ['doc1', 'doc2'], ['ref1', 'ref2']);

    waitDebounce();

    expect(apiClient.patch).toHaveBeenCalledTimes(1);
    expect(apiClient.patch).toHaveBeenCalledWith('/chat/sessions/s1', {
      context_ids: ['doc1', 'doc2', 'ref1', 'ref2'],
    });
    expect(getContextForSession('s1')).toEqual({ documentIds: ['doc1', 'doc2'], referenceIds: ['ref1', 'ref2'] });
  });

  it('rolls back Zustand and shows toast on PATCH failure', async () => {
    const showToast = vi.fn();
    vi.mocked(apiClient.patch).mockRejectedValue(new Error('Network error'));
    setupBridge({ showToast, getReferenceIdSet: () => new Set(['oldRef', 'ref1']) });

    hydrateFromSessions([{ session_id: 's1', context_ids: ['oldDoc', 'oldRef'] } as any]);

    setContextForSession('s1', ['doc1'], ['ref1']);
    vi.advanceTimersByTime(250);

    expect(apiClient.patch).toHaveBeenCalledTimes(1);

    // Flush microtasks so the .catch handler runs
    await vi.runAllTimersAsync();

    expect(getContextForSession('s1')).toEqual({ documentIds: ['oldDoc'], referenceIds: ['oldRef'] });
    expect(showToast).toHaveBeenCalledWith('Failed to save chat context', 'error');
  });

  it('calls updateChatSession on PATCH success', async () => {
    const updateChatSession = vi.fn();
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 's1' });
    setupBridge({ updateChatSession });

    setContextForSession('s1', ['doc1'], []);
    vi.advanceTimersByTime(250);

    // Flush microtasks
    await vi.runAllTimersAsync();

    expect(updateChatSession).toHaveBeenCalledWith('s1', { session_id: 's1' });
  });
});

describe('pruneContext', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setupBridge();
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 'test' });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(apiClient.patch).mockClear();
  });

  it('removes stale document IDs and deleted reference IDs', () => {
    setupBridge({ getReferenceIdSet: () => new Set(['aliveRef', 'deletedRef']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['alive', 'dead', 'aliveRef', 'deletedRef'],
    } as any]);

    const aliveDocs = new Set(['alive']);
    const deletedRefs = new Set(['deletedRef']);

    pruneContext('s1', aliveDocs, deletedRefs);

    expect(getContextForSession('s1')).toEqual({
      documentIds: ['alive'],
      referenceIds: ['aliveRef'],
    });
  });

  it('does nothing when context is already clean', () => {
    setupBridge({ getReferenceIdSet: () => new Set(['aliveRef']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['alive', 'aliveRef'],
    } as any]);

    const aliveDocs = new Set(['alive']);
    const deletedRefs = new Set<string>();

    const prev = getContextForSession('s1');
    pruneContext('s1', aliveDocs, deletedRefs);
    expect(getContextForSession('s1')).toEqual(prev);
  });
});

describe('resolveCompletionContext', () => {
  beforeEach(() => {
    setupBridge();
    // Clear any leaked pending state from earlier suites before merge-based hydrate.
    clearContextForSession('s1');
    hydrateFromSessions([]);
  });

  it('returns context arrays when active session has context', () => {
    setupBridge({ getReferenceIdSet: () => new Set(['ref1']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['doc1', 'ref1'],
    } as any]);

    const result = resolveCompletionContext();
    expect(result).toEqual({ context_ids: ['doc1', 'ref1'] });
  });

  it('returns empty arrays when session is null', () => {
    setupBridge({ getActiveSessionId: () => null });
    const result = resolveCompletionContext();
    expect(result).toEqual({ context_ids: [] });
  });
});

describe('clearContextForSession', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setupBridge();
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 'test' });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(apiClient.patch).mockClear();
  });

  it('removes context and cancels pending PATCH', () => {
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['doc1'],
    } as any]);

    setContextForSession('s1', ['doc2'], []);
    clearContextForSession('s1');
    vi.advanceTimersByTime(250);

    expect(apiClient.patch).not.toHaveBeenCalled();
    expect(getContextForSession('s1')).toEqual({ documentIds: [], referenceIds: [] });
  });
});

describe('hydrateFromSessions', () => {
  it('fills context store from session list', () => {
    setupBridge({ getReferenceIdSet: () => new Set(['r1']) });
    hydrateFromSessions([
      { session_id: 's1', context_ids: ['d1', 'r1'] } as any,
      { session_id: 's2', context_ids: ['d2'] } as any,
    ]);

    expect(getContextForSession('s1')).toEqual({ documentIds: ['d1'], referenceIds: ['r1'] });
    expect(getContextForSession('s2')).toEqual({ documentIds: ['d2'], referenceIds: [] });
  });
});

// Bridge consumers must fail loud when unwired.
describe('bridge readiness guard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetChatContextBridge();
  });

  afterEach(() => {
    vi.useRealTimers();
    setupBridge();
  });

  it('resolveCompletionContext surfaces an error when bridge is unwired', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    // No setupChatContextBridge call — bridge is unwired.
    const result = resolveCompletionContext();

    expect(errSpy).toHaveBeenCalled();
    // Still returns a sane (empty) shape — no pretend success with stale data.
    expect(result).toEqual({ context_ids: [] });
    errSpy.mockRestore();
  });

  it('setContextForSession flush surfaces an error when bridge is unwired', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 's1' });

    setContextForSession('s1', ['d1'], []);
    vi.advanceTimersByTime(250);
    await vi.runAllTimersAsync();

    // flushPatch ran while unwired — error logged, not a silent success.
    expect(errSpy).toHaveBeenCalled();
    errSpy.mockRestore();
  });
});

// Hydration ordering invariant: references must load before sessions hydrate.
describe('hydrateFromSessions refIdSet warning', () => {
  afterEach(() => {
    setupBridge();
  });

  it('warns when refIdSet is empty but sessions carry context_ids', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // Empty ref set simulates references not yet loaded into app-store.
    setupBridge({ getReferenceIdSet: () => new Set() });

    hydrateFromSessions([{ session_id: 's1', context_ids: ['r1', 'r2'] } as any]);

    expect(warnSpy).toHaveBeenCalled();
    // Still stores sane data (all-docs) matching current behavior — no silent change.
    expect(getContextForSession('s1')).toEqual({ documentIds: ['r1', 'r2'], referenceIds: [] });
    warnSpy.mockRestore();
  });

  it('does not warn when refIdSet is populated', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    setupBridge({ getReferenceIdSet: () => new Set(['r1']) });

    hydrateFromSessions([{ session_id: 's1', context_ids: ['r1'] } as any]);

    expect(warnSpy).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});
