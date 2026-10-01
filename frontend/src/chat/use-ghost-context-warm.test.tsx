/**
 * Failure-escalation tests for useGhostContextWarm.
 *
 * A failed first-circle fetch must degrade LOUDLY, never silently (a
 * bare-id-only derived selector with NO signal is a formal
 * no-silent-degradation violation). The warm effect escalates minimally: one
 * console.warn per failed fetch (dev-loud) and a SINGLE debounced toast per
 * warm cycle (not per
 * fetch — warm cycles are frequent; a per-fetch toast would be noisy on transients).
 *
 * The selector itself (useGhostChatContext) is covered by use-ghost-context.test.tsx;
 * this file isolates the failure path with rejecting fetchers + a real linkCache.
 *
 * The second describe covers the always-fresh base contract (plan
 * ghost-context-stale-links): while the chat is a ghost, its context follows the
 * open entity's CURRENT links — cached base targets are refetched on every warm
 * run and on editor-doc-changed, with the _rerunLinks lost-update guard keeping at
 * most one request per key in flight.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Mutable store state the mocked hooks + .getState() read. vi.hoisted keeps the
// reference stable across the hoisted vi.mock factories.
const mocks = vi.hoisted(() => ({
  appState: {
    currentDocument: { document_id: 'doc-1' } as { document_id: string } | null,
    currentReference: null as { reference_id: string } | null,
    showToast: vi.fn(),
  },
  uiState: {
    documents: {} as Record<string, { splitView?: boolean; refOpenMode?: 'center' | 'split' | 'panel' }>,
    language: 'en' as const,
  },
  chatState: { activeSessionId: null as string | null },
}));

// Real-shape linkCache mock state: one Map + a version counter + a listener set, so
// tests that render useGhostChatContext get useSyncExternalStore reactivity and the
// resolving fetcher mock can write the cache + notify exactly like fetchAndCache.
const linkMock = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const st = {
    cache: new Map<string, unknown>(),
    listeners,
    version: 0,
    bump: () => {
      st.version += 1;
      for (const cb of listeners) cb();
    },
  };
  return st;
});

// useAppStore / useUIStore must be callable hooks AND expose .getState():
//   - useAppStore(s => ...)            (reactive open-entity read)
//   - useAppStore.getState().showToast (non-reactive toast path)
//   - useUIStore(s => ...)             (reactive split flag)
//   - useUIStore.getState().language   (standalone t() resolves the dict)
vi.mock('../store/app-store', () => {
  const fn = (sel: (s: unknown) => unknown) => sel(mocks.appState);
  fn.getState = () => mocks.appState;
  return { useAppStore: fn };
});
vi.mock('../store/ui-store', () => {
  const fn = (sel: (s: unknown) => unknown) => sel(mocks.uiState);
  fn.getState = () => mocks.uiState;
  return { useUIStore: fn };
});

// The hook gates the editor-doc-changed rewarm on
// useChatStore.getState().activeSessionId === null (ghost gate) — mutable so a test
// can materialize a session.
vi.mock('../store/chat-store', () => {
  const fn = (sel: (s: unknown) => unknown) => sel(mocks.chatState);
  fn.getState = () => mocks.chatState;
  return { useChatStore: fn };
});

// Rejecting fetchers + a REAL linkCache Map so the warm loop runs (cache empty → fetch).
// subscribeLinkCache/getLinkCacheVersion mirror api/links.ts so the derived selector
// recomputes when a resolving fetcher mock writes the cache.
vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(() => Promise.reject(new Error('net'))),
  fetchReferenceLinksFresh: vi.fn(() => Promise.reject(new Error('net'))),
  linkCache: linkMock.cache,
  subscribeLinkCache: (cb: () => void) => {
    linkMock.listeners.add(cb);
    return () => { linkMock.listeners.delete(cb); };
  },
  getLinkCacheVersion: () => linkMock.version,
}));

import { useGhostContextWarm, useGhostChatContext, __resetLinkWarmState } from './use-ghost-context';
import { linkCache, fetchDocumentLinksFresh, fetchReferenceLinksFresh } from '../api/links';
import { emit } from '../events';
import { __resetGhostDeltaStore, addGhostDelta } from './context';
import type { FirstCircle } from '../utils/cascade-selection';

const cache = linkCache as unknown as Map<string, FirstCircle>;

function renderWarm(): () => void {
  const container = document.createElement('div');
  const root = createRoot(container);
  function Harness() {
    useGhostContextWarm();
    return createElement('div');
  }
  act(() => root.render(createElement(Harness)));
  return () => act(() => root.unmount());
}

function renderWarmWithSelector(): { unmount: () => void; getContext: () => { documentIds: string[]; referenceIds: string[] } } {
  const container = document.createElement('div');
  const root = createRoot(container);
  let ctx: { documentIds: string[]; referenceIds: string[] } = { documentIds: [], referenceIds: [] };
  function Harness() {
    useGhostContextWarm();
    ctx = useGhostChatContext();
    return createElement('div');
  }
  act(() => root.render(createElement(Harness)));
  return { unmount: () => act(() => root.unmount()), getContext: () => ctx };
}

beforeEach(() => {
  vi.useFakeTimers();
  cache.clear();
  __resetGhostDeltaStore();
  mocks.appState.currentDocument = { document_id: 'doc-1' };
  mocks.appState.currentReference = null;
  mocks.uiState.documents = {};
  mocks.uiState.language = 'en';
  mocks.appState.showToast = vi.fn();
  vi.mocked(fetchDocumentLinksFresh).mockClear();
  vi.mocked(fetchReferenceLinksFresh).mockClear();
});

// Runs AFTER the beforeEach above (registration order): resets any per-test fetcher
// override back to the factory default (rejecting) so a resolving implementation
// from one test never leaks into another, and clears the ghost-gate + version state.
// Also clears the module warm state (a previous test may end with a fetch pending)
// and any leaked linkCache listeners from an un-unmounted harness.
beforeEach(() => {
  // Restore a console.warn spy leaked by a previous test (the cancelled-guard test
  // in the suite above never restores its spy): vitest's nested spyOn would inherit
  // its recorded calls into the next test's spy and poison warn-count assertions.
  vi.spyOn(console, 'warn').mockRestore();
  vi.mocked(fetchDocumentLinksFresh).mockReset();
  vi.mocked(fetchReferenceLinksFresh).mockReset();
  vi.mocked(fetchDocumentLinksFresh).mockImplementation(() => Promise.reject(new Error('net')));
  vi.mocked(fetchReferenceLinksFresh).mockImplementation(() => Promise.reject(new Error('net')));
  mocks.chatState.activeSessionId = null;
  linkMock.version = 0;
  linkMock.listeners.clear();
  __resetLinkWarmState();
});

describe('useGhostContextWarm — failure escalation (plan #4)', () => {
  it('warns and shows a single debounced toast when a first-circle fetch fails', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

    const unmount = renderWarm();
    await act(async () => { await vi.runAllTimersAsync(); });

    // console.warn fired (dev-loud) for the failed doc-1 fetch.
    expect(warnSpy).toHaveBeenCalled();
    // Exactly ONE toast — debounced per warm cycle, not per failed fetch.
    expect(mocks.appState.showToast).toHaveBeenCalledTimes(1);
    expect(mocks.appState.showToast).toHaveBeenCalledWith('Some linked context could not be loaded', 'error');
    warnSpy.mockRestore();
    unmount();
  });

  it('coalesces multiple failures into one toast (debounced per warm cycle)', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    // Split view with both panes open → two base targets (doc-1 + ref-1). This
    // avoids the ghost delta store, which syncGhostBaseKey resets on mount.
    mocks.appState.currentReference = { reference_id: 'ref-1' };
    mocks.uiState.documents = { 'doc-1': { refOpenMode: 'split' } };

    const unmount = renderWarm();
    await act(async () => { await vi.runAllTimersAsync(); });

    // Two failed fetches → two console.warn (per fetch), but still ONE toast.
    expect(warnSpy).toHaveBeenCalledTimes(2);
    expect(mocks.appState.showToast).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
    unmount();
  });

  it('does not toast after unmount before the debounce fires (cancelled guard)', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});

    const unmount = renderWarm();
    // Advance just past the catch's microtask flush (which schedules the 500ms toast
    // timer) but NOT to 500ms, so the toast has not fired yet.
    await act(async () => { await vi.advanceTimersByTimeAsync(499); });
    // Unmount runs the effect cleanup: cancelled=true + clears the pending toast timer.
    unmount();
    await act(async () => { await vi.runAllTimersAsync(); });

    expect(mocks.appState.showToast).not.toHaveBeenCalled();
  });
});

// ─── Always-fresh base contract ──────────────────────────────────────────

const EMPTY_CIRCLE: FirstCircle = { document_ids: [], reference_ids: [] };

/** Resolving doc fetcher: writes the cache + bumps the version at RESOLUTION time
 * (after a microtask yield), like the real fetchAndCache writes after its await. */
function resolveDocLinksWith(...results: FirstCircle[]): void {
  let i = 0;
  vi.mocked(fetchDocumentLinksFresh).mockImplementation(async (id: string) => {
    const fc = results[Math.min(i++, results.length - 1)];
    await null;
    cache.set(`doc:${id}`, fc);
    linkMock.bump();
    return fc;
  });
}

/** Deferred doc fetcher with concurrency tracking (lost-update guard tests). */
function trackDocFetches(): {
  state: { calls: number; inflight: number; maxInflight: number };
  settleNext: (fc?: FirstCircle) => Promise<void>;
  rejectNext: () => Promise<void>;
} {
  const state = { calls: 0, inflight: 0, maxInflight: 0 };
  const pending: Array<{ resolve: (fc: FirstCircle) => void; reject: (e: Error) => void }> = [];
  vi.mocked(fetchDocumentLinksFresh).mockImplementation((id: string) => {
    state.calls += 1;
    state.inflight += 1;
    state.maxInflight = Math.max(state.maxInflight, state.inflight);
    return new Promise<FirstCircle>((resolve, reject) => {
      pending.push({
        resolve: fc => { state.inflight -= 1; cache.set(`doc:${id}`, fc); linkMock.bump(); resolve(fc); },
        reject: e => { state.inflight -= 1; reject(e); },
      });
    });
  });
  // Resolve/reject + drain the microtask chain (.catch passthrough → .finally →
  // any rerun fetch fires synchronously inside finally) inside ONE act, so the
  // cache-write bump's store update is act-wrapped and call counts are stable.
  const settle = async (fn: () => void) => act(async () => {
    fn();
    for (let i = 0; i < 10; i++) await Promise.resolve();
  });
  return {
    state,
    settleNext: async (fc: FirstCircle = EMPTY_CIRCLE) => { await settle(() => { pending.shift()!.resolve(fc); }); },
    rejectNext: async () => { await settle(() => { pending.shift()!.reject(new Error('net')); }); },
  };
}

describe('useGhostContextWarm — always-fresh base while ghost', () => {
  it('refetches a cached base target on mount and derives the fresh (emptied) first circle', async () => {
    // Symptom (2): an emptied document never invalidates in-editor, so the cached
    // entry {B,C} survives a delete-all + reopen.
    cache.set('doc:doc-1', { document_ids: ['B', 'C'], reference_ids: [] });
    resolveDocLinksWith(EMPTY_CIRCLE);
    const h = renderWarmWithSelector();
    // Cache hit IGNORED for base targets: the fetcher fired for doc-1 anyway.
    expect(fetchDocumentLinksFresh).toHaveBeenCalledWith('doc-1');
    // Stale-while-revalidate: until the fresh response lands, the cached circle
    // still shows (no bare-id flash).
    expect(h.getContext()).toEqual({ documentIds: ['doc-1', 'B', 'C'], referenceIds: [] });
    await act(async () => { await vi.runAllTimersAsync(); });
    // Fresh response = empty lists → the derived context is doc-1 only.
    expect(h.getContext()).toEqual({ documentIds: ['doc-1'], referenceIds: [] });
    h.unmount();
  });

  it('re-warms the base on editor-doc-changed while the chat is a ghost', async () => {
    resolveDocLinksWith(
      { document_ids: ['B'], reference_ids: [] },
      { document_ids: [], reference_ids: [] },
    );
    const h = renderWarmWithSelector();
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(fetchDocumentLinksFresh).toHaveBeenCalledTimes(1);
    expect(h.getContext()).toEqual({ documentIds: ['doc-1', 'B'], referenceIds: [] });

    act(() => { emit('editor-doc-changed'); });
    await act(async () => { await vi.runAllTimersAsync(); });

    // Fresh fetcher called again, and the new result replaced the cache entry.
    expect(fetchDocumentLinksFresh).toHaveBeenCalledTimes(2);
    expect(cache.get('doc:doc-1')).toEqual(EMPTY_CIRCLE);
    expect(h.getContext()).toEqual({ documentIds: ['doc-1'], referenceIds: [] });
    h.unmount();
  });

  it('does not re-warm on editor-doc-changed once a session is materialized', async () => {
    resolveDocLinksWith(EMPTY_CIRCLE);
    const h = renderWarmWithSelector();
    await act(async () => { await vi.runAllTimersAsync(); });
    mocks.chatState.activeSessionId = 's1';
    act(() => { emit('editor-doc-changed'); });
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(fetchDocumentLinksFresh).toHaveBeenCalledTimes(1); // mount fetch only
    h.unmount();
  });

  it('refetches exactly once when the event fires while the base fetch is pending', async () => {
    const trk = trackDocFetches();
    const h = renderWarmWithSelector();
    expect(trk.state.calls).toBe(1); // mount fetch in flight
    act(() => { emit('editor-doc-changed'); });
    expect(trk.state.calls).toBe(1); // pending → mark rerun, no parallel request
    await trk.settleNext();
    expect(trk.state.calls).toBe(2); // exactly one extra fetch after settle
    await trk.settleNext();          // settle the rerun so no key stays pending
    expect(trk.state.maxInflight).toBe(1);
    h.unmount();
  });

  it('keeps skip-if-cached for manual-add targets (deltas are not refetched)', async () => {
    resolveDocLinksWith(EMPTY_CIRCLE);
    const h = renderWarmWithSelector();
    await act(async () => { await vi.runAllTimersAsync(); });
    cache.set('doc:doc-9', EMPTY_CIRCLE);
    // Manual add re-runs the warm effect; doc-9 is cached → skipped. The delta is
    // added AFTER mount so syncGhostBaseKey's baseKey reset does not wipe it.
    act(() => { addGhostDelta('doc', 'doc-9'); });
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(fetchDocumentLinksFresh).not.toHaveBeenCalledWith('doc-9');
    // The base itself IS refetched by the effect re-run (always-fresh base).
    expect(fetchDocumentLinksFresh).toHaveBeenCalledWith('doc-1');
    h.unmount();
  });

  it('never runs two doc-1 fetches in parallel: reruns serialize and the last event wins', async () => {
    const trk = trackDocFetches();
    const h = renderWarmWithSelector();
    expect(trk.state.calls).toBe(1);
    act(() => { emit('editor-doc-changed'); }); // event 1 → mark rerun
    act(() => { emit('editor-doc-changed'); }); // event 2 → mark is idempotent
    expect(trk.state.calls).toBe(1);
    await trk.settleNext(); // settle #1 → finally → rerun fires #2
    expect(trk.state.calls).toBe(2);
    act(() => { emit('editor-doc-changed'); }); // event 3 while the rerun is in flight
    await trk.settleNext(); // settle #2 → finally → final fetch #3
    expect(trk.state.calls).toBe(3);
    await trk.settleNext(); // settle #3 → no mark → chain stops
    expect(trk.state.calls).toBe(3);
    expect(trk.state.maxInflight).toBe(1); // never more than one in flight
    h.unmount();
  });

  it('warns and shows one debounced toast when an event-driven fetch fails', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    resolveDocLinksWith(EMPTY_CIRCLE);
    const h = renderWarmWithSelector();
    await act(async () => { await vi.runAllTimersAsync(); }); // mount fetch OK
    expect(warnSpy).not.toHaveBeenCalled();
    vi.mocked(fetchDocumentLinksFresh).mockImplementation(() => Promise.reject(new Error('net')));
    act(() => { emit('editor-doc-changed'); });
    await act(async () => { await vi.runAllTimersAsync(); });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(mocks.appState.showToast).toHaveBeenCalledTimes(1);
    expect(mocks.appState.showToast).toHaveBeenCalledWith('Some linked context could not be loaded', 'error');
    warnSpy.mockRestore();
    h.unmount();
  });

  it('warns but does not toast when an event-driven fetch rejects after unmount', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const trk = trackDocFetches();
    const h = renderWarmWithSelector();
    await trk.settleNext(); // mount fetch OK
    act(() => { emit('editor-doc-changed'); }); // event fetch #2 in flight
    expect(trk.state.calls).toBe(2);
    h.unmount(); // before settle and before the 500ms debounce fires
    await trk.rejectNext(); // fetch #2 rejects post-unmount
    await act(async () => { await vi.runAllTimersAsync(); }); // debounce fires
    expect(warnSpy).toHaveBeenCalledTimes(1); // dev-loud warn still lands
    expect(mocks.appState.showToast).not.toHaveBeenCalled(); // toast suppressed
    warnSpy.mockRestore();
  });
});
