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
import { fetchDocumentLinksFresh, fetchReferenceLinksFresh } from '../api/links';
import {
  setupChatContextBridge,
  resetChatContextBridge,
  getContextForSession,
  setContextForSession,
  clearContextForSession,
  hydrateFromSessions,
  pruneContext,
  resolveCompletionContext,
  getDerivedGhostContext,
  addItemToContext,
  removeItemFromContext,
  computeContextPrune,
  GHOST_SESSION_ID,
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

  // The open-entity fallback was REMOVED: the
  // ghost derives its own context, and a materialized session keeps its stored
  // snapshot. An empty stored context now sends empty — no re-derivation at send.
  it('returns empty arrays when the active session has no stored context', () => {
    setupBridge({ getActiveSessionId: () => 's1' });
    const result = resolveCompletionContext();
    expect(result).toEqual({ context_ids: [] });
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

describe('addItemToContext / removeItemFromContext', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    setupBridge();
    vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 's1' });
    vi.mocked(fetchDocumentLinksFresh).mockReset();
    vi.mocked(fetchReferenceLinksFresh).mockReset();
    hydrateFromSessions([{ session_id: 's1', context_ids: [] } as any]);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(apiClient.patch).mockClear();
  });

  it('addItemToContext writes target sync, then merges first-circle async', async () => {
    vi.mocked(fetchDocumentLinksFresh).mockResolvedValue({ document_ids: ['d2'], reference_ids: ['r1'] });

    const p = addItemToContext('s1', 'doc', 'd1');

    // Sync step already happened — target is in context.
    expect(getContextForSession('s1')).toEqual({ documentIds: ['d1'], referenceIds: [] });

    await vi.runAllTimersAsync();
    await p;

    expect(getContextForSession('s1')).toEqual({ documentIds: ['d1', 'd2'], referenceIds: ['r1'] });
  });

  it('removeItemFromContext drops target sync, then drops first-circle async', async () => {
    setupBridge({ getReferenceIdSet: () => new Set(['r1', 'r2']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['d1', 'd2', 'd3', 'r1', 'r2'],
    } as any]);
    vi.mocked(fetchDocumentLinksFresh).mockResolvedValue({ document_ids: ['d2'], reference_ids: ['r1'] });

    const p = removeItemFromContext('s1', 'doc', 'd1');

    // Sync step — only the target is gone yet.
    expect(getContextForSession('s1')).toEqual({ documentIds: ['d2', 'd3'], referenceIds: ['r1', 'r2'] });

    await vi.runAllTimersAsync();
    await p;

    expect(getContextForSession('s1')).toEqual({ documentIds: ['d3'], referenceIds: ['r2'] });
  });

  it('removeItemFromContext on a child does NOT touch the parent', async () => {
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['parent', 'child'],
    } as any]);
    // Child's own first-circle points to deeper docs that are NOT in selection.
    vi.mocked(fetchDocumentLinksFresh).mockResolvedValue({ document_ids: ['grandchild'], reference_ids: [] });

    await removeItemFromContext('s1', 'doc', 'child');
    await vi.runAllTimersAsync();

    expect(getContextForSession('s1')).toEqual({ documentIds: ['parent'], referenceIds: [] });
  });

  it('addItemToContext for a ref pulls in its linked docs and refs', async () => {
    vi.mocked(fetchReferenceLinksFresh).mockResolvedValue({ document_ids: ['d1', 'd2'], reference_ids: ['r2'] });

    await addItemToContext('s1', 'ref', 'r1');
    await vi.runAllTimersAsync();

    const ctx = getContextForSession('s1');
    expect(ctx.documentIds.sort()).toEqual(['d1', 'd2']);
    expect(ctx.referenceIds.sort()).toEqual(['r1', 'r2']);
  });

  it('async cascade reads CURRENT state, not a stale snapshot', async () => {
    // Slow doc fetch: another op lands on s1 before it resolves.
    let resolveLinks: (v: FirstCircleLike) => void = () => {};
    vi.mocked(fetchDocumentLinksFresh).mockImplementationOnce(() => new Promise(res => { resolveLinks = res; }));
    vi.mocked(fetchReferenceLinksFresh).mockResolvedValue({ document_ids: [], reference_ids: [] });

    const slow = addItemToContext('s1', 'doc', 'parent');
    // Sync step ran: ['parent'].
    expect(getContextForSession('s1').documentIds).toEqual(['parent']);

    // Meanwhile user adds an independent ref.
    await addItemToContext('s1', 'ref', 'rIndep');
    await vi.runAllTimersAsync();
    expect(getContextForSession('s1').referenceIds).toEqual(['rIndep']);

    // Now resolve the slow links — child appears, rIndep must survive.
    resolveLinks({ document_ids: ['child'], reference_ids: [] });
    await slow;
    await vi.runAllTimersAsync();

    const ctx = getContextForSession('s1');
    expect(ctx.documentIds.sort()).toEqual(['child', 'parent']);
    expect(ctx.referenceIds).toEqual(['rIndep']);
  });

  it('shows toast and keeps sync result when fetch fails', async () => {
    const showToast = vi.fn();
    setupBridge({ showToast });
    vi.mocked(fetchDocumentLinksFresh).mockRejectedValue(new Error('net'));

    await addItemToContext('s1', 'doc', 'd1');
    await vi.runAllTimersAsync();

    // Target was added in the sync step; cascade never ran.
    expect(getContextForSession('s1').documentIds).toEqual(['d1']);
    expect(showToast).toHaveBeenCalledWith('Failed to load links', 'error');
  });

  // _cascadeEpoch decision: after the ghost refactor, applyCascade
  // serves ONLY materialized sessions (ghost uses deltas). clearContextForSession is
  // still called on a materialized session being DELETED (sessions-slice.deleteSession).
  // If a picker cascade is in flight when the session is deleted, the late merge must
  // NOT re-create the bucket and PATCH a deleted session (→ 404 → spurious "Failed to
  // save chat context" toast). The epoch guard drops it. This pins the guard's other
  // job — it is NOT ghost-coupled and must stay.
  it('a cascade whose materialized session is deleted mid-fetch does not PATCH it', async () => {
    vi.mocked(apiClient.patch).mockClear();
    let resolveLinks: (v: FirstCircleLike) => void = () => {};
    vi.mocked(fetchDocumentLinksFresh).mockImplementationOnce(() => new Promise(res => { resolveLinks = res; }));

    // Picker toggle on a live session → cascade in flight (sync step wrote 'd1').
    void addItemToContext('s1', 'doc', 'd1');
    expect(getContextForSession('s1').documentIds).toEqual(['d1']);

    // User deletes the chat before the fetch resolves → clearContextForSession bumps epoch.
    clearContextForSession('s1');
    expect(getContextForSession('s1')).toEqual({ documentIds: [], referenceIds: [] });

    // The stale cascade resolves late — it must be dropped, not merged + PATCHed.
    resolveLinks({ document_ids: ['child'], reference_ids: [] });
    await vi.runAllTimersAsync();

    expect(getContextForSession('s1')).toEqual({ documentIds: [], referenceIds: [] });
    const s1Patches = vi.mocked(apiClient.patch).mock.calls.filter(
      (c: unknown[]) => typeof c[0] === 'string' && c[0].includes('/s1'),
    );
    expect(s1Patches).toHaveLength(0);
  });
});

type FirstCircleLike = { document_ids: string[]; reference_ids: string[] };

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

  // ─── id-invariant server split ─
  // The server's context_reference_ids is the single source of truth for the
  // doc/ref split. The open document's reference scope (refIdSet) must NEVER be
  // evidence of a reference's existence — a cross-doc ref is permanently outside
  // that scope and would otherwise flip into documentIds on every doc switch.

  it('uses server context_reference_ids even when the ref is absent from refIdSet', () => {
    // refIdSet simulates the OPEN doc's scope — it does NOT contain 'refCross'.
    setupBridge({ getReferenceIdSet: () => new Set(['refLocal']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['docA', 'refCross', 'refLocal'],
      context_reference_ids: ['refCross', 'refLocal'],
    } as any]);
    // The cross-doc ref classifies as a reference (server authority), not a doc.
    expect(getContextForSession('s1')).toEqual({
      documentIds: ['docA'],
      referenceIds: ['refCross', 'refLocal'],
    });
  });

  it('falls back to refIdSet split when context_reference_ids is absent (older server)', () => {
    setupBridge({ getReferenceIdSet: () => new Set(['ref1']) });
    hydrateFromSessions([{
      session_id: 's1',
      context_ids: ['doc1', 'ref1'],
      // context_reference_ids deliberately omitted → legacy path.
    } as any]);
    expect(getContextForSession('s1')).toEqual({
      documentIds: ['doc1'],
      referenceIds: ['ref1'],
    });
  });

  it('persistence regression: selection membership is stable across an open-doc switch', () => {
    // A reference NOT in the open doc's scope stays a reference on re-hydrate.
    // Today (no server field) the split flips → the References tab checkbox
    // visibly un-checks itself ("partial reset").
    let refIdSet = new Set(['refA']);
    setupBridge({ getReferenceIdSet: () => refIdSet });
    const session = {
      session_id: 's1',
      context_ids: ['docA', 'docB', 'refA', 'refCross'],
      context_reference_ids: ['refA', 'refCross'],
    } as any;
    hydrateFromSessions([session]);
    const before = getContextForSession('s1');
    // Simulate a re-hydrate driven by the references-changed subscription after a
    // doc switch — scope still excludes refCross.
    refIdSet = new Set(['refA']);
    hydrateFromSessions([session]);
    const after = getContextForSession('s1');
    expect(after).toEqual(before);
    expect(after).toEqual({
      documentIds: ['docA', 'docB'],
      referenceIds: ['refA', 'refCross'],
    });
  });

  it('PATCH order stability: identical context_ids order regardless of open-doc scope', () => {
    // flushPatch sends context_ids = [...docIds, ...refIds]. A stable split
    // means that flattened order is invariant to which document is open. Today
    // the flipping split reorders context_ids server-side on every doc switch.
    const session = {
      session_id: 's1',
      context_ids: ['docA', 'refA', 'refCross'],
      context_reference_ids: ['refA', 'refCross'],
    } as any;

    setupBridge({ getReferenceIdSet: () => new Set(['refA', 'refCross']) });
    hydrateFromSessions([session]);
    const orderWithScope = [
      ...getContextForSession('s1').documentIds,
      ...getContextForSession('s1').referenceIds,
    ];

    // Switch to a doc whose ancestor scope excludes refCross.
    setupBridge({ getReferenceIdSet: () => new Set(['refA']) });
    hydrateFromSessions([session]);
    const orderWithoutScope = [
      ...getContextForSession('s1').documentIds,
      ...getContextForSession('s1').referenceIds,
    ];

    expect(orderWithoutScope).toEqual(orderWithScope);
    expect(orderWithScope).toEqual(['docA', 'refA', 'refCross']);
  });

  describe('merge semantics (F4 fix)', () => {
    beforeEach(() => {
      vi.useFakeTimers();
      setupBridge();
      vi.mocked(apiClient.patch).mockResolvedValue({ session_id: 's1' });
      clearContextForSession('s1');
      clearContextForSession('s2');
    });
    afterEach(() => {
      vi.useRealTimers();
      vi.mocked(apiClient.patch).mockClear();
      clearContextForSession('s1');
      clearContextForSession('s2');
    });

    it('hydrate_preserves_pending_patch_for_session_X', () => {
      // Server snapshot has stale value; user optimistically added newer one.
      // Pending patch must win.
      hydrateFromSessions([{ session_id: 's1', context_ids: ['stale'] } as any]);
      setContextForSession('s1', ['optimistic'], []);

      // Re-hydrate with the same stale server snapshot before debounce fires.
      hydrateFromSessions([{ session_id: 's1', context_ids: ['stale'] } as any]);

      expect(getContextForSession('s1')).toEqual({ documentIds: ['optimistic'], referenceIds: [] });
    });

    it('hydrate_overwrites_when_no_pending', () => {
      setupBridge({ getReferenceIdSet: () => new Set(['newRef']) });
      hydrateFromSessions([{ session_id: 's1', context_ids: ['old'] } as any]);
      hydrateFromSessions([{ session_id: 's1', context_ids: ['new', 'newRef'] } as any]);
      expect(getContextForSession('s1')).toEqual({ documentIds: ['new'], referenceIds: ['newRef'] });
    });

    it('hydrate_drops_sessions_not_in_response', () => {
      hydrateFromSessions([
        { session_id: 's1', context_ids: ['d1'] } as any,
        { session_id: 's2', context_ids: ['d2'] } as any,
      ]);
      // Second hydrate without s2 — it should be pruned.
      hydrateFromSessions([{ session_id: 's1', context_ids: ['d1'] } as any]);
      expect(getContextForSession('s2')).toEqual({ documentIds: [], referenceIds: [] });
    });

    it('hydrate_keeps_pending_session_not_in_response', () => {
      // PATCH not yet flushed for s2; GET response doesn't include s2 yet.
      setContextForSession('s2', ['pending'], []);
      hydrateFromSessions([{ session_id: 's1', context_ids: ['d1'] } as any]);
      expect(getContextForSession('s2')).toEqual({ documentIds: ['pending'], referenceIds: [] });
    });

     // Regression: the ghost context is now
     // DERIVED — nothing writes a GHOST_SESSION_ID bucket, so a re-loadSessions has
     // no ghost attach to preserve. (The over-inclusion race this once guarded is
     // structurally impossible: there is no stored bucket to merge onto.)
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

  // Mirrors resolveCompletionContext: getDerivedGhostContext (the materialization
  // snapshot) must fail loud when the bridge is unwired, not silently snapshot an
  // empty ghost context onto the first turn. Symmetry with the consumer above.
  it('getDerivedGhostContext surfaces an error when bridge is unwired', () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    // No setupChatContextBridge call — bridge is unwired.
    const result = getDerivedGhostContext();

    expect(errSpy).toHaveBeenCalled();
    // Still returns a sane (empty) shape — no crash, no pretend success.
    expect(result).toEqual({ docIds: [], refIds: [] });
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

// ─── ChatInput prune-effect decision ─
// computeContextPrune is the PURE gate the ChatInput prune effect calls. Returning
// null means "do nothing" — the effect must NOT call pruneContext / setContextForSession
// (which would PATCH away context). WHY: a context id is pruned only when the
// SERVER says which ids are references.
// Why: the open document's reference scope is never evidence of a reference's
// existence (a cross-doc ref is permanently outside it), so pruning by scope would
// drop a cross-doc reference id silently — only the server's referenceIds is authoritative.
describe('computeContextPrune (A.5)', () => {
  const docs = (ids: string[]) => ids.map(document_id => ({ document_id }));

  it('a cross-doc reference (in referenceIds, not deleted, absent from the open scope) survives — no prune', () => {
    // documents = the whole project; the cross-doc ref is NOT a document.
    const decision = computeContextPrune(
      { documentIds: ['docA'], referenceIds: ['crossRef'] },
      ['crossRef'],
      docs(['docA']),
      new Set(), // not deleted
    );
    expect(decision).toBeNull();
  });

  it('a deleted document IS pruned when context_reference_ids is present', () => {
    const decision = computeContextPrune(
      { documentIds: ['docA', 'deadDoc'], referenceIds: [] },
      [],
      docs(['docA']), // deadDoc absent from the live documents list
      new Set(),
    );
    expect(decision).not.toBeNull();
    expect([...decision!.aliveDocIds].sort()).toEqual(['docA']);
  });

  it('a deleted document IS pruned while references is still empty (documents loaded) — pins the relaxed guard', () => {
    // The guard does not wait on `references`; documents present is enough.
    const decision = computeContextPrune(
      { documentIds: ['deadDoc'], referenceIds: [] },
      [],
      docs(['otherDoc']),
      new Set(),
    );
    expect(decision).not.toBeNull();
  });

  it('nothing is pruned while documents is empty', () => {
    const decision = computeContextPrune(
      { documentIds: ['docA'], referenceIds: ['refA'] },
      ['refA'],
      [], // documents not loaded yet
      new Set(['refA']),
    );
    expect(decision).toBeNull();
  });

  it('skips entirely when context_reference_ids is absent (older server / not yet loaded)', () => {
    // No PATCH can drop a live cross-doc ref whose split is untrusted.
    const decision = computeContextPrune(
      { documentIds: ['docA'], referenceIds: ['refA'] },
      undefined,
      docs(['docA']),
      new Set(),
    );
    expect(decision).toBeNull();
  });

  it('a soft-deleted reference IS pruned (deletedRefIds)', () => {
    const decision = computeContextPrune(
      { documentIds: ['docA'], referenceIds: ['refA', 'refDel'] },
      ['refA', 'refDel'],
      docs(['docA']),
      new Set(['refDel']),
    );
    expect(decision).not.toBeNull();
    expect(decision!.deletedRefIds.has('refDel')).toBe(true);
  });

  it('does not prune when context is already clean', () => {
    const decision = computeContextPrune(
      { documentIds: ['docA'], referenceIds: ['refA'] },
      ['refA'],
      docs(['docA']),
      new Set(),
    );
    expect(decision).toBeNull();
  });
});
