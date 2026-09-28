/**
 * Failure-escalation tests for useGhostContextWarm.
 *
 * The warm effect used to `.catch(() => {})` silently: a failed first-circle
 * fetch degraded the derived selector to bare-id-only with NO signal — a formal
 * no-silent-degradation violation. It now escalates minimally: one console.warn
 * per failed fetch (dev-loud) and a SINGLE debounced toast per warm cycle (not per
 * fetch — warm cycles are frequent; a per-fetch toast would be noisy on transients).
 *
 * The selector itself (useGhostChatContext) is covered by use-ghost-context.test.tsx;
 * this file isolates the failure path with rejecting fetchers + a real linkCache.
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
}));

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

// Rejecting fetchers + a REAL linkCache Map so the warm loop runs (cache empty → fetch).
vi.mock('../api/links', () => ({
  fetchDocumentLinksFresh: vi.fn(() => Promise.reject(new Error('net'))),
  fetchReferenceLinksFresh: vi.fn(() => Promise.reject(new Error('net'))),
  linkCache: new Map(),
}));

import { useGhostContextWarm } from './use-ghost-context';
import { linkCache, fetchDocumentLinksFresh, fetchReferenceLinksFresh } from '../api/links';
import { __resetGhostDeltaStore } from './context';
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
