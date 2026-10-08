/** Tests for useEditorReferenceSync — per-source ownership of the transclude map +
 * the content_flushed chattiness gate. Drives the real Zustand stores + a mocked apiClient
 * via a minimal renderHook (no @testing-library/react). */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// The custom renderHook (no @testing-library/react) doesn't set the act environment flag,
// which spams "not configured to support act(...)" warnings despite correct act() usage.
// Opt in explicitly so the test output stays clean.
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn().mockResolvedValue([]),
    post: vi.fn().mockResolvedValue({ items: [] }),
  },
}));
// fetchReferences fires its own network call unrelated to the rebuild — stub it out.
vi.mock('../editor/editor-utils', () => ({ fetchReferences: vi.fn() }));

import { useAppStore } from '../store/app-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { useEditorReferenceSync } from './useEditorReferenceSync';
import { useReferenceEvents } from './useReferenceEvents';
import { useDeletedRefIds } from '../store/deleted-ref-ids';
import { transcludeMap, projectRefIds, missingRefIds } from '../components/editor/live-preview';
import { apiClient } from '../api/client';
import { clearPreviewCache } from './useDocumentPreview';
import { clearRefPreviewCache, fetchRefPreview, seedRefPreview } from './useReferencePreview';
import { emit } from '../events';
import type { Reference } from '../types';

let container: HTMLDivElement;
let root: Root;

function renderHook(editorViewRef?: React.RefObject<unknown>, withRefEvents = false) {
  container = document.createElement('div');
  document.body.appendChild(container);
  const Wrapper = () => {
    const localRef = useRef(null);
    useEditorReferenceSync({ editorViewRef: (editorViewRef ?? localRef) as never });
    if (withRefEvents) useReferenceEvents();
    return null;
  };
  root = createRoot(container);
  act(() => { root.render(createElement(Wrapper)); });
}

function flushMicrotasks() {
  return act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

const imgRef = (id: string, title = id) => ({
  reference_id: id, title, media_type: 'image' as const,
  file_meta: { original_name: `${id}.png` }, is_reference: true, project_id: 'p',
}) as unknown as Reference;
const textRef = (id: string) => ({
  reference_id: id, title: id, media_type: 'markdown' as const,
  content: `text of ${id}`, is_reference: true, project_id: 'p',
}) as unknown as Reference;
// A metadata-only list ref: the server LIST drops content; has_content signals the
// body exists server-side and must be lazy-fetched.
const metaRef = (id: string) => ({
  reference_id: id, title: id, media_type: 'markdown' as const,
  content: undefined, has_content: true, is_reference: true, project_id: 'p',
}) as unknown as Reference;

/** A minimal editor-view spy with the fields the hook touches: dispatch + a doc the
 * lazy-fetch effect can read (`view.state.doc.toString()`). */
function spyView(dispatch = vi.fn()): { dispatch: ReturnType<typeof vi.fn> } {
  return { dispatch, state: { doc: { toString: () => '' } } } as never;
}
const doc = (id: string, title = id, content?: string) => ({
  document_id: id, title, project_id: 'p', ...(content !== undefined ? { content } : {}),
});

describe('useEditorReferenceSync — per-source ownership', () => {
  beforeEach(() => {
    transcludeMap.clear();
    projectRefIds.clear();
    missingRefIds.clear();
    clearPreviewCache();
    clearRefPreviewCache();
    useAppStore.getState().setReferences([]);
    useAppStore.getState().setDocuments([]);
    useAppStore.setState({
      currentProject: { project_id: 'p', index_doc_id: 'idx' } as never,
      currentDocument: { document_id: 'cur' } as never,
    });
    useNoteChatStore.setState({ sessions: [] } as never);
    (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    (apiClient.post as ReturnType<typeof vi.fn>).mockReset();
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(endpoint === '/references/resolve' ? [] : { items: [] }),
    );
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    act(() => root.unmount());
    container.remove();
  });

  it('an ancestor store mutation does NOT wipe a project-ref entry', async () => {
    // The resolver seeds cross-doc refs found in the open text via POST
    // /references/resolve. Have it seed sibling-ref as project-ref, then prove an
    // ancestor rebuild leaves it intact.
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(
        endpoint === '/references/resolve' ? [textRef('sibling-ref')] : { items: [] },
      ),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[note](ref:sibling-ref)';
    renderHook({ current: view } as never);
    await act(async () => { vi.advanceTimersByTime(400); });
    await flushMicrotasks();
    expect(transcludeMap.get('sibling-ref')?.source).toBe('project-ref');
    // A store mutation (ancestor scope) triggers the rebuild — project-ref must survive.
    act(() => { useAppStore.getState().setReferences([imgRef('own')]); });
    await flushMicrotasks();
    const entry = transcludeMap.get('sibling-ref');
    expect(entry).toBeDefined();
    expect(entry!.source).toBe('project-ref');
    expect(entry!.content).toBe('text of sibling-ref');
  });

  // ─── project-ref resolver ────────────────────────────────────────────────────

  /** Drive the debounced resolve pass and let its POST settle. */
  async function runResolvePass() {
    await act(async () => { vi.advanceTimersByTime(400); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
  }

  function resolveCalls(): [string, unknown][] {
    return (apiClient.post as ReturnType<typeof vi.fn>).mock.calls
      .filter((c: unknown[]) => c[0] === '/references/resolve') as [string, unknown][];
  }

  it('an id in text not in validRefIds is POSTed once; the missing verdict sticks', async () => {
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:cross1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    expect(resolveCalls()).toEqual([
      ['/references/resolve', { project_id: 'p', ids: ['cross1'] }],
    ]);
    expect(missingRefIds.has('cross1')).toBe(true);
    // Next change (keystroke → editor-doc-changed): NOT re-POSTed.
    act(() => { emit('editor-doc-changed'); });
    await runResolvePass();
    expect(resolveCalls().length).toBe(1);
  });

  it('a found id lands in projectRefIds and as a project-ref entry', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(
        endpoint === '/references/resolve' ? [textRef('found1')] : { items: [] },
      ),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:found1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    expect(projectRefIds.has('found1')).toBe(true);
    expect(missingRefIds.has('found1')).toBe(false);
    const entry = transcludeMap.get('found1');
    expect(entry?.source).toBe('project-ref');
    expect(entry?.content).toBe('text of found1');
    expect(view.dispatch).toHaveBeenCalled();
  });

  it('an id already in validRefIds (ancestor store) is not POSTed at all', async () => {
    act(() => { useAppStore.getState().setReferences([textRef('own1')]); });
    await flushMicrotasks();
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:own1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    expect(resolveCalls().length).toBe(0);
  });

  it('both link forms collect ids: [x](ref:a) and ![](ref:b) in one POST', async () => {
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:x1) and ![](ref:x2)';
    renderHook({ current: view } as never);
    await runResolvePass();
    const calls = resolveCalls();
    expect(calls.length).toBe(1);
    expect((calls[0][1] as { ids: string[] }).ids.sort()).toEqual(['x1', 'x2']);
  });

  it('no GET /references?project_id= call is made at all', async () => {
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:any1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    const calls = (apiClient.get as ReturnType<typeof vi.fn>).mock.calls.map((c: unknown[]) => c[0]);
    expect(calls.some((c) => typeof c === 'string' && c.startsWith('/references?project_id='))).toBe(false);
  });

  it('ws:reference_created for a missing id re-resolves it', async () => {
    let resolveAnswer: Reference[] = [];
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(endpoint === '/references/resolve' ? resolveAnswer : { items: [] }),
    );
    // fetchRef for the created ref FAILS → addReference never runs → the only path
    // to validity is the resolver re-POST (what this test isolates).
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(
        typeof endpoint === 'string' && endpoint.startsWith('/references/gone1')
          ? Promise.reject(new Error('net'))
          : [],
      ),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:gone1)';
    renderHook({ current: view } as never, true /* mount useReferenceEvents */);
    await runResolvePass();
    expect(resolveCalls().length).toBe(1);
    expect(missingRefIds.has('gone1')).toBe(true);
    resolveAnswer = [textRef('gone1')];
    act(() => {
      emit('ws:reference_created', {
        reference_id: 'gone1', title: 'Gone', document_id: null,
        created_by: null, created_by_name: null,
      });
    });
    await flushMicrotasks();
    await runResolvePass();
    expect(resolveCalls().length).toBe(2);
    expect(projectRefIds.has('gone1')).toBe(true);
    expect(transcludeMap.get('gone1')?.source).toBe('project-ref');
  });

  it('ws:reference_deleted drops the resolved id and its project-ref entry', async () => {
    let resolveAnswer: Reference[] = [textRef('del1')];
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(endpoint === '/references/resolve' ? resolveAnswer : { items: [] }),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:del1)';
    renderHook({ current: view } as never, true /* mount useReferenceEvents */);
    await runResolvePass();
    expect(transcludeMap.get('del1')?.source).toBe('project-ref');
    resolveAnswer = [];
    act(() => { emit('ws:reference_deleted', { reference_id: 'del1' }); });
    await runResolvePass();
    expect(projectRefIds.has('del1')).toBe(false);
    expect(transcludeMap.get('del1')).toBeUndefined();
    expect(missingRefIds.has('del1')).toBe(true);
  });

  it.each([
    ['ws:reference_deleted', () => emit('ws:reference_deleted', { reference_id: 'img1' })],
    ['ws:documents_deleted_batch', () => emit('ws:documents_deleted_batch', { document_ids: [], reference_ids: ['img1'] })],
  ])('%s feeds the page-wide deleted set (the chat image plates read it)', (_label, fire) => {
    useDeletedRefIds.setState({ ids: new Set() });
    renderHook(undefined, true /* mount useReferenceEvents */);
    act(() => { fire(); });
    expect(useDeletedRefIds.getState().ids.has('img1')).toBe(true);
  });

  it('a project switch clears both Sets and project-ref entries', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(
        endpoint === '/references/resolve' ? [textRef('sw1')] : { items: [] },
      ),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:sw1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    expect(projectRefIds.has('sw1')).toBe(true);
    expect(transcludeMap.get('sw1')?.source).toBe('project-ref');
    act(() => {
      useAppStore.setState({ currentProject: { project_id: 'p2', index_doc_id: 'idx2' } as never });
    });
    await flushMicrotasks();
    expect(projectRefIds.has('sw1')).toBe(false);
    expect(missingRefIds.has('sw1')).toBe(false);
    expect(transcludeMap.get('sw1')).toBeUndefined();
  });

  it('a resolve response that lands after a project switch is dropped', async () => {
    let answer!: (refs: Reference[]) => void;
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      endpoint === '/references/resolve'
        ? new Promise((r) => { answer = r; })
        : Promise.resolve({ items: [] }),
    );
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:late1) [y](ref:late2)';
    renderHook({ current: view } as never);
    await act(async () => { vi.advanceTimersByTime(400); });
    expect(resolveCalls().length).toBe(1);
    act(() => {
      useAppStore.setState({ currentProject: { project_id: 'p2', index_doc_id: 'idx2' } as never });
    });
    await flushMicrotasks();
    await act(async () => { answer([textRef('late1')]); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
    expect(projectRefIds.has('late1')).toBe(false);
    expect(missingRefIds.has('late2')).toBe(false);
    expect(transcludeMap.get('late1')).toBeUndefined();
  });

  it('a failing resolve toasts once per failure streak, not on every editing pause', async () => {
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      endpoint === '/references/resolve'
        ? Promise.reject(new Error('network'))
        : Promise.resolve({ items: [] }),
    );
    const showToastSpy = vi.spyOn(useAppStore.getState(), 'showToast');
    const view = spyView();
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '[x](ref:fail1)';
    renderHook({ current: view } as never);
    await runResolvePass();
    act(() => { emit('editor-doc-changed'); });
    await runResolvePass();
    act(() => { emit('editor-doc-changed'); });
    await runResolvePass();
    expect(resolveCalls().length).toBe(3);
    expect(showToastSpy.mock.calls.filter((c) => c[1] === 'error').length).toBe(1);
  });

  it('a Task-5 content reload of a doc entry SURVIVES the next storeDocuments rebuild', async () => {
    renderHook();
    // Seed a doc entry that has already been content-loaded (a prior Task-5 / lazy reload).
    transcludeMap.set('d1', { kind: 'doc', source: 'doc', title: 'D1', content: 'fetched body' });
    // storeDocuments now carries the doc WITHOUT content (tree metadata) — the rebuild must
    // NOT revert it to a loading entry; the fetched content is carried forward.
    act(() => { useAppStore.getState().setDocuments([doc('d1', 'D1') as never]); });
    await flushMicrotasks();
    const entry = transcludeMap.get('d1');
    expect(entry?.content).toBe('fetched body');
    expect(entry?.source).toBe('doc');
  });

  it('a content-mutating write preserves the entry’s original source', async () => {
    renderHook();
    transcludeMap.set('d2', { kind: 'doc', source: 'doc', title: 'D2', content: 'old' });
    act(() => { useAppStore.getState().setDocuments([doc('d2', 'D2') as never]); });
    await flushMicrotasks();
    // Simulate a content_flushed reload writing fresh content into the existing entry.
    act(() => {
      const cur = transcludeMap.get('d2')!;
      transcludeMap.set('d2', { ...cur, content: 'fresh' });
    });
    expect(transcludeMap.get('d2')?.content).toBe('fresh');
    expect(transcludeMap.get('d2')?.source).toBe('doc');
  });

  // ─── carryContent: a ref band survives a metadata-only rebuild (blink fix) ────
  // The lazy-fetched body lives ONLY in transcludeMap (never written back to the store).
  // A storeReferences identity change (optimistic removeReference + WS confirm on delete;
  // prefetch/SWR/network mergeReferences on doc switch) used to drop it back to LOADING and
  // re-fetch 300ms later → a visible down/up blink. carryContent overlays the fetched body
  // onto a metadata-only rebuild for BOTH refs and docs, so the band never flips to loading.

  it('a lazily-fetched ref-text entry SURVIVES the next storeReferences rebuild', async () => {
    renderHook();
    // Seed an entry whose body was already lazy-fetched (the batch/lazy effect).
    transcludeMap.set('r', { kind: 'ref-text', source: 'ancestor-ref', title: 'r', content: 'fetched body' });
    // storeReferences now carries the ref as metadata-only (LIST drops content).
    act(() => { useAppStore.getState().setReferences([metaRef('r')]); });
    await flushMicrotasks();
    // The fetched body is carried forward — NOT reset to the loading spinner.
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
  });

  it('rebuild dispatches nothing on a content-identical storeReferences change (no blink)', async () => {
    const view = spyView();
    const editorViewRef = { current: view } as never;
    renderHook(editorViewRef);
    transcludeMap.set('r', { kind: 'ref-text', source: 'ancestor-ref', title: 'r', content: 'fetched body' });
    act(() => { useAppStore.getState().setReferences([metaRef('r')]); });
    await flushMicrotasks();
    // First change: the map gains the entry → one dispatch.
    expect(view.dispatch).toHaveBeenCalled();
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
    const callsAfterFirst = (view.dispatch as ReturnType<typeof vi.fn>).mock.calls.length;
    // Second, identity-only change: mergeReferences allocates a NEW array with identical
    // content → storeReferences identity changes → effect re-runs, but the diff gate finds
    // everything identical → NO second dispatch (blink suppressed).
    act(() => { useAppStore.getState().mergeReferences([metaRef('r')]); });
    await flushMicrotasks();
    expect((view.dispatch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsAfterFirst);
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
  });

  it('deleting an unrelated ref preserves a surviving band’s fetched body', async () => {
    const view = spyView();
    const editorViewRef = { current: view } as never;
    renderHook(editorViewRef);
    transcludeMap.set('r', { kind: 'ref-text', source: 'ancestor-ref', title: 'r', content: 'fetched body' });
    act(() => { useAppStore.getState().setReferences([metaRef('r'), metaRef('other')]); });
    await flushMicrotasks();
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
    // Optimistic delete of an UNRELATED ref: validRefIds genuinely changes, so a dispatch
    // IS correct here — but the surviving band must re-render already-loaded, not loading.
    act(() => { useAppStore.getState().removeReference('other'); });
    await flushMicrotasks();
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
    const callsAfterDelete = (view.dispatch as ReturnType<typeof vi.fn>).mock.calls.length;
    // WS confirm: removeReference('other') again (id already absent). Array.filter returns a
    // NEW array (same content) → storeReferences identity changes → effect re-runs, but the
    // diff gate now finds everything identical → NO second dispatch (blink #2 suppressed).
    act(() => { useAppStore.getState().removeReference('other'); });
    await flushMicrotasks();
    expect((view.dispatch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsAfterDelete);
    expect(transcludeMap.get('r')?.content).toBe('fetched body');
  });

  it('a ref whose store content arrives is updated (no stale body)', async () => {
    renderHook();
    transcludeMap.set('r', { kind: 'ref-text', source: 'ancestor-ref', title: 'r', content: 'old lazy' });
    // The store now carries fresh content → it must win over the stale lazy body.
    act(() => { useAppStore.getState().setReferences([textRef('r')]); });
    await flushMicrotasks();
    expect(transcludeMap.get('r')?.content).toBe('text of r');
  });

  it('a transcluded sibling-doc band’s title follows the store title (not the fetched-time title)', async () => {
    renderHook();
    // Seed a doc entry whose captured title is stale; the store carries the authoritative title.
    transcludeMap.set('d1', { kind: 'doc', source: 'doc', title: 'Stale Title', content: 'fetched body' });
    act(() => { useAppStore.getState().setDocuments([doc('d1', 'Fresh Title') as never]); });
    await flushMicrotasks();
    const entry = transcludeMap.get('d1');
    // Content is carried forward (no blink) AND the title follows the store.
    expect(entry?.content).toBe('fetched body');
    expect(entry?.title).toBe('Fresh Title');
  });

  it('content_flushed for an id present only as a ref-text entry triggers NO doc fetch (gate)', async () => {
    renderHook();
    // A transcluded reference lives as ref-text, not doc — the gate must skip it.
    transcludeMap.set('rtext', { kind: 'ref-text', source: 'project-ref', title: 'R', content: 'c' });
    await flushMicrotasks();
    act(() => { emit('ws:content_flushed', { entity_id: 'rtext', entity_type: 'doc' }); });
    await flushMicrotasks();
    act(() => { vi.advanceTimersByTime(500); });
    // apiClient.get for /documents/rtext must never have been called.
    const calls = (apiClient.get as ReturnType<typeof vi.fn>).mock.calls.map((c: unknown[]) => c[0]);
    expect(calls.some((c) => typeof c === 'string' && c.includes('/documents/rtext'))).toBe(false);
  });

  it('content_flushed for a reference evicts its cached preview body (next hover re-fetches)', async () => {
    renderHook(undefined, true);
    seedRefPreview('rflush', { title: 'R', content: 'old body' });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      reference_id: 'rflush', title: 'R', media_type: 'markdown', content: 'new body',
    });
    act(() => { emit('ws:content_flushed', { entity_id: 'rflush', entity_type: 'doc', is_reference: true }); });
    await flushMicrotasks();
    await expect(fetchRefPreview('rflush')).resolves.toMatchObject({ content: 'new body' });
  });

  it('content_flushed for the CURRENTLY OPEN doc triggers NO server fetch (local buffer wins)', async () => {
    // The open editor buffer is authoritative and may carry unsaved edits newer than the
    // flushed (saved) snapshot — a server reload would overwrite the band with stale content.
    renderHook();
    const openId = useAppStore.getState().currentDocument?.document_id as string;
    transcludeMap.set(openId, { kind: 'doc', source: 'doc', title: 'Open', content: 'local' });
    await flushMicrotasks();
    act(() => { emit('ws:content_flushed', { entity_id: openId, entity_type: 'doc' }); });
    await flushMicrotasks();
    act(() => { vi.advanceTimersByTime(500); });
    const calls = (apiClient.get as ReturnType<typeof vi.fn>).mock.calls.map((c: unknown[]) => c[0]);
    expect(calls.some((c) => typeof c === 'string' && c.includes(`/documents/${openId}`))).toBe(false);
    // The band entry is left untouched (no stale overwrite).
    expect(transcludeMap.get(openId)?.content).toBe('local');
  });

  it('a content-less has_content text ref becomes a LOADING entry (not dropped)', async () => {
    // Regression for the metadata-only LIST contract: a text ref whose content is
    // not yet loaded must seed a ref-text loading entry (content undefined) — NOT
    // return null (which would render the embed as a broken link).
    renderHook();
    act(() => { useAppStore.getState().setReferences([metaRef('lazy')]); });
    await flushMicrotasks();
    const entry = transcludeMap.get('lazy');
    expect(entry).toBeDefined();
    expect(entry!.kind).toBe('ref-text');
    expect(entry!.content).toBeUndefined();
  });

  it('the lazy-fetch effect fills a content-less ref-text entry from the content batch', async () => {
    // The lazy-fetch effect collects ref: ids whose entry is still loading
    // and resolves them through ONE POST /documents/batch (replacing the per-id
    // GET /references/{id}), updating the entry in place (content-mutating writer,
    // source preserved).
    (apiClient.post as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      endpoint === '/documents/batch'
        ? Promise.resolve({ items: [{ document_id: 'lazy', media_type: 'markdown', content: 'text of lazy' }] })
        : Promise.resolve({ items: [] }),
    );
    const view = spyView();
    const editorViewRef = { current: view } as never;
    renderHook(editorViewRef);
    // Seed the loading entry from the store list.
    act(() => { useAppStore.getState().setReferences([metaRef('lazy')]); });
    await flushMicrotasks();
    expect(transcludeMap.get('lazy')?.content).toBeUndefined();
    // The editor text references the ref so the lazy-fetch effect picks it up.
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '![lazy](ref:lazy)';
    // Drive the debounce + microtasks.
    await act(async () => { vi.advanceTimersByTime(500); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
    expect(transcludeMap.get('lazy')?.content).toBe('text of lazy');
  });

  it('dispatches linkContextChanged on the editor view when the map content changes', async () => {    // The hook dispatches a `linkContextChanged` StateEffect on the editorViewRef whenever
    // the diff gate detects a real map change. Inject a spy view to observe the dispatch.
    const view = spyView();
    const editorViewRef = { current: view } as never;
    renderHook(editorViewRef);
    act(() => { useAppStore.getState().setReferences([textRef('t1')]); });
    await flushMicrotasks();
    expect(transcludeMap.has('t1')).toBe(true);
    expect(view.dispatch).toHaveBeenCalled();
    const effectArg = (view.dispatch as ReturnType<typeof vi.fn>).mock.calls[0][0];
    // The dispatch is { effects: [linkContextChanged.of(null)] }.
    expect(effectArg).toHaveProperty('effects');
  });

  it('concurrent content_flushed for two docs reloads BOTH (per-entity debounce)', async () => {
    // Regression: a single shared timer let a later doc's flush cancel an earlier doc's
    // pending reload, leaving the first band stale. The debounce must be keyed per entity.
    (apiClient.get as ReturnType<typeof vi.fn>).mockImplementation((endpoint: string) =>
      Promise.resolve(
        endpoint.includes('/documents/dA') ? { content: 'body A' } :
        endpoint.includes('/documents/dB') ? { content: 'body B' } :
        [],
      ),
    );
    const editorViewRef = { current: spyView() } as never;
    renderHook(editorViewRef);
    transcludeMap.set('dA', { kind: 'doc', source: 'doc', title: 'A', content: 'old A' });
    transcludeMap.set('dB', { kind: 'doc', source: 'doc', title: 'B', content: 'old B' });
    await flushMicrotasks();
    // Two docs flush within the same 300ms window.
    act(() => { emit('ws:content_flushed', { entity_id: 'dA', entity_type: 'doc' }); });
    act(() => { emit('ws:content_flushed', { entity_id: 'dB', entity_type: 'doc' }); });
    await act(async () => { vi.runAllTimers(); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
    expect(transcludeMap.get('dA')?.content).toBe('body A');
    expect(transcludeMap.get('dB')?.content).toBe('body B');
  });

  it('content_flushed reload failure surfaces an error toast (no silent degradation)', async () => {
    // Regression (5cf1411): the content_flushed reload must reject on fetch failure and
    // surface a toast instead of silently keeping stale pre-edit content.
    (apiClient.get as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('network'));
    const showToastSpy = vi.spyOn(useAppStore.getState(), 'showToast');
    const editorViewRef = { current: spyView() } as never;
    renderHook(editorViewRef);
    transcludeMap.set('dFail', { kind: 'doc', source: 'doc', title: 'D', content: 'old body' });
    await flushMicrotasks();
    act(() => { emit('ws:content_flushed', { entity_id: 'dFail', entity_type: 'doc' }); });
    await flushMicrotasks();
    await act(async () => { vi.runAllTimers(); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
    expect(showToastSpy).toHaveBeenCalledWith(expect.any(String), 'error');
    // The band is left with its prior content (not wiped to loading/empty).
    expect(transcludeMap.get('dFail')?.content).toBe('old body');
    showToastSpy.mockRestore();
  });

  it('the lazy-fetch effect resolves N cold doc ids with ONE batch POST (not N per-id GETs)', async () => {
    // The two per-id loops collapse
    // into a single POST /documents/batch for all cold ids in one debounced pass.
    (apiClient.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      items: [
        { document_id: 'da', content: 'body A', media_type: 'markdown' },
        { document_id: 'db', content: 'body B', media_type: 'markdown' },
      ],
    });
    const view = spyView();
    const editorViewRef = { current: view } as never;
    renderHook(editorViewRef);
    // Make both ids valid project docs and seed their transclude entries as cold (loading).
    act(() => {
      useAppStore.getState().setDocuments([doc('da', 'A') as never, doc('db', 'B') as never]);
    });
    transcludeMap.set('da', { kind: 'doc', source: 'doc', title: 'A' });
    transcludeMap.set('db', { kind: 'doc', source: 'doc', title: 'B' });
    (view as unknown as { state: { doc: { toString: () => string } } })
      .state.doc.toString = () => '![da](doc:da) ![db](doc:db)';
    await act(async () => { vi.advanceTimersByTime(500); });
    await act(async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); });
    // Exactly ONE batch POST for both ids.
    expect(apiClient.post).toHaveBeenCalledTimes(1);
    const [endpoint, body] = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(endpoint).toBe('/documents/batch');
    expect((body as { ids: string[] }).ids.sort()).toEqual(['da', 'db']);
    // Entries filled from the batch response.
    expect(transcludeMap.get('da')?.content).toBe('body A');
    expect(transcludeMap.get('db')?.content).toBe('body B');
  });
});
