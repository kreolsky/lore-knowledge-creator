/**
 * Render test for useGhostChatContext.
 *
 * Locks the reactive wiring: the hook reads app/ui state + the real ghost-delta
 * store + linkCache, maps deriveGhostContext's {docIds,refIds} to ChatContext
 * {documentIds,referenceIds}, and recomputes when (a) the open entity changes,
 * (b) a ghost delta mutates, or (c) linkCache warms + the cache-version bumps.
 * The pure selector itself is covered by ghost-context.test.ts.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { createElement, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Mutable app/ui state the mocked store selectors read. vi.hoisted keeps the
// reference stable across the hoisted vi.mock factory.
const mocks = vi.hoisted(() => ({
  appState: {
    currentDocument: { document_id: 'doc-1' } as { document_id: string } | null,
    currentReference: null as { reference_id: string } | null,
  },
  uiState: {
    documents: {} as Record<string, { splitView?: boolean; refOpenMode?: 'center' | 'split' | 'panel' }>,
  },
}));

vi.mock('../store/app-store', () => ({ useAppStore: (sel: (s: unknown) => unknown) => sel(mocks.appState) }));
vi.mock('../store/ui-store', () => ({ useUIStore: (sel: (s: unknown) => unknown) => sel(mocks.uiState) }));

// use-ghost-context reads the ghost gate via useChatStore.getState() in its
// editor-doc-changed handler; this suite never emits that event, but the module
// import must resolve — mock the store (the real chat-store's module init needs
// useAppStore.subscribe, which the app-store mock above does not provide).
vi.mock('../store/chat-store', () => {
  const fn = (sel: (s: unknown) => unknown) => sel({ activeSessionId: null });
  fn.getState = () => ({ activeSessionId: null });
  return { useChatStore: fn };
});

// REAL links.ts over a mocked apiClient: warming via the fetcher writes the cache AND
// bumps the shared version the derived selector subscribes to (keystone test).
vi.mock('../api/client', () => ({ apiClient: { get: vi.fn() } }));

import { useGhostChatContext } from './use-ghost-context';
import { addGhostDelta, __resetGhostDeltaStore } from './context';
import { linkCache, fetchDocumentLinksFresh } from '../api/links';
import { apiClient } from '../api/client';
import type { FirstCircle } from '../utils/cascade-selection';

const cache = linkCache as unknown as Map<string, FirstCircle>;

function resetState() {
  mocks.appState.currentDocument = { document_id: 'doc-1' };
  mocks.appState.currentReference = null;
  mocks.uiState.documents = {};
}

function renderHook(): { current: () => unknown; rerender: () => void; unmount: () => void } {
  const container = document.createElement('div');
  let value: unknown = undefined;
  let forceUpdate: () => void = () => {};
  function Harness() {
    const [, setN] = useState(0);
    forceUpdate = () => setN(n => n + 1);
    value = useGhostChatContext();
    return createElement('div');
  }
  const root: Root = createRoot(container);
  act(() => root.render(createElement(Harness)));
  return {
    current: () => value,
    rerender: () => act(() => forceUpdate()),
    unmount: () => act(() => root.unmount()),
  };
}

beforeEach(() => {
  cache.clear();
  __resetGhostDeltaStore();
  resetState();
});

describe('useGhostChatContext', () => {
  it('maps the derived value to ChatContext {documentIds, referenceIds}', () => {
    cache.set('doc:doc-1', { document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });
    const h = renderHook();
    expect(h.current()).toEqual({ documentIds: ['doc-1', 'linked-doc'], referenceIds: ['linked-ref'] });
    h.unmount();
  });

  it('recomputes when the open entity changes', () => {
    const h = renderHook();
    expect((h.current() as { documentIds: string[] }).documentIds).toEqual(['doc-1']);
    mocks.appState.currentReference = { reference_id: 'ref-1' };
    h.rerender();
    expect(h.current()).toEqual({ documentIds: [], referenceIds: ['ref-1'] });
    h.unmount();
  });

  it("'split' mode folds document + open reference into the base", () => {
    mocks.appState.currentReference = { reference_id: 'ref-1' };
    mocks.uiState.documents = { 'doc-1': { refOpenMode: 'split' } };
    const h = renderHook();
    expect(h.current()).toEqual({ documentIds: ['doc-1'], referenceIds: ['ref-1'] });
    h.unmount();
  });

  // Panel is a QUICK PREVIEW: the reference is a viewer, not the scope — the
  // ghost base is the document's alone (refIsScope projection in the hook).
  it("'panel' mode derives the document-only context (previewed reference is not the scope)", () => {
    mocks.appState.currentReference = { reference_id: 'ref-1' };
    mocks.uiState.documents = { 'doc-1': { refOpenMode: 'panel' } };
    const h = renderHook();
    expect(h.current()).toEqual({ documentIds: ['doc-1'], referenceIds: [] });
    h.unmount();
  });

  it("a legacy persisted {splitView: true} still derives the split context", () => {
    mocks.appState.currentReference = { reference_id: 'ref-1' };
    mocks.uiState.documents = { 'doc-1': { splitView: true } };
    const h = renderHook();
    expect(h.current()).toEqual({ documentIds: ['doc-1'], referenceIds: ['ref-1'] });
    h.unmount();
  });

  it('recomputes reactively when a ghost delta mutates (zustand subscription)', () => {
    const h = renderHook();
    expect((h.current() as { documentIds: string[] }).documentIds).toEqual(['doc-1']);
    act(() => addGhostDelta('doc', 'extra-doc'));
    expect((h.current() as { documentIds: string[] }).documentIds).toEqual(['doc-1', 'extra-doc']);
    h.unmount();
  });

  it('recomputes when linkCache warms via a fetch + the shared version bumps (keystone)', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ document_ids: ['linked-doc'], reference_ids: ['linked-ref'] });
    const h = renderHook();
    expect(h.current()).toEqual({ documentIds: ['doc-1'], referenceIds: [] });
    await act(async () => { await fetchDocumentLinksFresh('doc-1'); });
    expect(h.current()).toEqual({ documentIds: ['doc-1', 'linked-doc'], referenceIds: ['linked-ref'] });
    h.unmount();
  });
});
