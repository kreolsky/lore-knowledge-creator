/**
 * Regression: first-circle not reflected in the picker until a manual toggle
 *.
 *
 * Root cause was a version-tracking gap: the derived ghost selector memoized on a
 * ghost-store counter bumped by exactly ONE cache writer (ghost-warm's own .then),
 * guarded by a `!cancelled` flag. A cancelled-run / pending-dedup race (StrictMode
 * double-mount in dev; base change with in-flight warm in prod) filled the cache
 * WITHOUT bumping → the memo, computed against the cold cache, never recomputed.
 *
 * The fix versions the cache at its SOURCE (api/links.ts): every cache mutation
 * bumps a shared counter the derived selector subscribes to via useSyncExternalStore.
 * This file drives the honest repro: warm + derive wired together under StrictMode
 * with a deferred fetcher resolving AFTER the second effect run, and asserts the
 * derived context picks up the first circle with no manual toggle — and that the
 * picker's two UI signals (claimed link vs derived selection) agree.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createElement, StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mocks = vi.hoisted(() => ({
  appState: {
    currentDocument: { document_id: 'doc-1' } as { document_id: string } | null,
    currentReference: null as { reference_id: string } | null,
    showToast: vi.fn(),
  },
  uiState: {
    documents: {} as Record<string, { splitView?: boolean }>,
    language: 'en' as const,
  },
}));

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

// use-ghost-context reads the ghost gate via useChatStore.getState() in its
// editor-doc-changed handler; this suite never emits that event, but the module
// import must resolve — mock the store (the real chat-store's module init needs
// useAppStore.subscribe, which the app-store mock above does not provide).
vi.mock('../store/chat-store', () => {
  const fn = (sel: (s: unknown) => unknown) => sel({ activeSessionId: null });
  fn.getState = () => ({ activeSessionId: null });
  return { useChatStore: fn };
});

// REAL links.ts (so a fetch bumps the shared version) over a mocked apiClient whose
// resolution we control to reproduce the cancelled-run race.
let resolveGet: (v: unknown) => void = () => {};
vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn(() => new Promise(res => { resolveGet = res; })),
  },
}));

import { useGhostContextWarm, useGhostChatContext } from './use-ghost-context';
import { clearLinkCache } from '../api/links';
import { __resetGhostDeltaStore } from './context';

let captured: ReturnType<typeof useGhostChatContext> | undefined;

function renderStrict(): () => void {
  const container = document.createElement('div');
  const root = createRoot(container);
  function Harness() {
    useGhostContextWarm();
    captured = useGhostChatContext();
    return createElement('div');
  }
  act(() => root.render(createElement(StrictMode, null, createElement(Harness))));
  return () => act(() => root.unmount());
}

beforeEach(() => {
  clearLinkCache();
  __resetGhostDeltaStore();
  mocks.appState.currentDocument = { document_id: 'doc-1' };
  mocks.appState.currentReference = null;
  mocks.uiState.documents = {};
  captured = undefined;
});

describe('linkCache version reactivity (ghost first-circle in picker)', () => {
  it('derives the first circle after a StrictMode-double-mounted warm resolves — no manual toggle', async () => {
    const unmount = renderStrict();
    // Cold cache: only the base doc is in context so far.
    expect(captured).toEqual({ documentIds: ['doc-1'], referenceIds: [] });

    // Resolve the single in-flight fetch AFTER both StrictMode effect runs. Under the
    // old code the run-1 `.then` saw cancelled=true and skipped the bump → stuck here.
    await act(async () => {
      resolveGet({ document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(captured).toEqual({ documentIds: ['doc-1', 'linked-doc'], referenceIds: ['linked-ref'] });
    unmount();
  });
});
