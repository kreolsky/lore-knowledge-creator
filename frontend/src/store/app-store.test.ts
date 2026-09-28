/** Unit tests for Zustand stores — tree building, document switching, thread persistence, UI persistence. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useAppStore, clearHydrateInFlight } from './app-store';
import { useChatStore } from './chat-store';
import { useNoteChatStore } from './note-chat-store';
import { useNoteStore } from './note-store';
import { useUIStore } from './ui-store';
import * as uiStoreNS from './ui-store';
import * as logoutNS from './logout-handlers';
import * as refsFetchNS from '../api/references-fetch';
import { apiClient } from '../api/client';
import { t } from '../i18n';
import type { Document, Project, Reference, User } from '../types';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    document_id: 'doc-1',
    project_id: 'proj-1',
    parent_id: null,
    title: 'Test Doc',
    content: '',
    path: 'test.md',
    is_index: false,
    sort_key: 'a0',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    project_id: 'proj-1',
    name: 'Test Project',
    status: 'active',
    project_context: '',
    index_doc_id: null,
    voice_recording_doc_id: null,
    last_accessed_doc_id: null,
    owner_id: null,
    is_public: false,
    my_access: 'full',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeRef(overrides: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'ref-1',
    project_id: 'proj-1',
    document_id: null,
    title: 'Test Ref',
    media_type: 'markdown',
    source_url: null,
    content: '',
    processing_status: null,
    file_path: null,
    file_meta: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

// ── Setup ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  useAppStore.setState({
    currentProject: null,
    currentDocument: null,
    currentReference: null,
    currentTable: null,
    accessLevel: 'full',
    documents: [],
    documentTree: [],
    references: [],
    snapshotPreview: null,
    previewDocument: null,
    previewScrollOffset: null,
    liveHeadings: null,
  });
  useNoteStore.setState({
    activeNoteThreadId: null,
    documentNoteThreads: {},
    pendingNoteNavigation: null,
  });
  useUIStore.setState({
    documentPositions: {},
    selectionEmpty: true,
  });
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('buildDocumentTree (via setDocuments)', () => {
  it('builds nested tree from flat list using parent_id', () => {
    const parent = makeDoc({ document_id: 'parent', parent_id: null });
    const child = makeDoc({ document_id: 'child', parent_id: 'parent' });
    useAppStore.getState().setDocuments([parent, child]);
    const tree = useAppStore.getState().documentTree;
    expect(tree).toHaveLength(1);
    expect(tree[0].document_id).toBe('parent');
    expect(tree[0].children).toHaveLength(1);
    expect(tree[0].children[0].document_id).toBe('child');
  });

  it('excludes is_index documents from tree', () => {
    const index = makeDoc({ document_id: 'idx', is_index: true });
    const normal = makeDoc({ document_id: 'normal', parent_id: null });
    useAppStore.getState().setDocuments([index, normal]);
    const tree = useAppStore.getState().documentTree;
    expect(tree).toHaveLength(1);
    expect(tree[0].document_id).toBe('normal');
  });

  it('handles orphan documents (parent_id points to nonexistent)', () => {
    const orphan = makeDoc({ document_id: 'orphan', parent_id: 'nonexistent' });
    useAppStore.getState().setDocuments([orphan]);
    const tree = useAppStore.getState().documentTree;
    expect(tree).toHaveLength(0);
  });

  it('rebuilds documentTree automatically on setDocuments', () => {
    useAppStore.getState().setDocuments([makeDoc()]);
    expect(useAppStore.getState().documentTree).toHaveLength(1);
    useAppStore.getState().setDocuments([]);
    expect(useAppStore.getState().documentTree).toHaveLength(0);
  });

  it('orders root siblings by sort_key ascending regardless of input order', () => {
    const c = makeDoc({ document_id: 'c', parent_id: null, sort_key: 'a2' });
    const a = makeDoc({ document_id: 'a', parent_id: null, sort_key: 'a0' });
    const b = makeDoc({ document_id: 'b', parent_id: null, sort_key: 'a1' });
    useAppStore.getState().setDocuments([c, a, b]);
    const ids = useAppStore.getState().documentTree.map(n => n.document_id);
    expect(ids).toEqual(['a', 'b', 'c']);
  });

  it('orders child siblings by sort_key ascending', () => {
    const parent = makeDoc({ document_id: 'parent', parent_id: null, sort_key: 'a0' });
    const c2 = makeDoc({ document_id: 'c2', parent_id: 'parent', sort_key: 'a5' });
    const c1 = makeDoc({ document_id: 'c1', parent_id: 'parent', sort_key: 'a1' });
    useAppStore.getState().setDocuments([parent, c2, c1]);
    const childIds = useAppStore.getState().documentTree[0].children.map(n => n.document_id);
    expect(childIds).toEqual(['c1', 'c2']);
  });

  it('re-sorts the tree when only a sort_key changes (fingerprint includes sort_key)', () => {
    const a = makeDoc({ document_id: 'a', parent_id: null, sort_key: 'a0' });
    const b = makeDoc({ document_id: 'b', parent_id: null, sort_key: 'a1' });
    useAppStore.getState().setDocuments([a, b]);
    expect(useAppStore.getState().documentTree.map(n => n.document_id)).toEqual(['a', 'b']);
    // Move 'a' below 'b' by bumping its sort_key — same ids/parents, only sort_key differs
    useAppStore.getState().setDocuments([{ ...a, sort_key: 'a2' }, b]);
    expect(useAppStore.getState().documentTree.map(n => n.document_id)).toEqual(['b', 'a']);
  });
});

describe('setCurrentDocument', () => {
  it('clears currentReference when setting a document', () => {
    useAppStore.setState({ currentReference: makeRef() });
    expect(useAppStore.getState().currentReference).not.toBeNull();
    useAppStore.getState().setCurrentDocument(makeDoc());
    expect(useAppStore.getState().currentReference).toBeNull();
  });

  it('restores activeNoteThreadId from documentNoteThreads', () => {
    useNoteStore.setState({
      documentNoteThreads: { 'doc-1': 'thread-1' },
    });
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-1' }));
    expect(useNoteStore.getState().activeNoteThreadId).toBe('thread-1');
  });

  it('defaults activeNoteThreadId to null for unknown document', () => {
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'unknown' }));
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
  });

  it('preserves pendingNoteNavigation thread instead of saved thread', () => {
    useNoteStore.setState({
      documentNoteThreads: { 'doc-1': 'saved-thread' },
      pendingNoteNavigation: { noteId: 'pending-note' },
    });
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-1' }));
    expect(useNoteStore.getState().activeNoteThreadId).toBe('pending-note');
  });

  it('clears snapshotPreview on document switch', () => {
    useAppStore.setState({
      snapshotPreview: { checkpoint_id: 'cp1', document_id: 'doc-1', content: '', label: '', comment: '', created_at: '' },
    });
    useAppStore.getState().setCurrentDocument(makeDoc());
    expect(useAppStore.getState().snapshotPreview).toBeNull();
  });

  it('resets activeNoteThreadId to null when setting null document', () => {
    useNoteStore.setState({ activeNoteThreadId: 'some-thread' });
    useAppStore.getState().setCurrentDocument(null);
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
  });

  it('clears the search-tab overlay when the document changes', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'docA' }) });
    useUIStore.getState().setRightPanelTab('docA', 'search');
    expect(useUIStore.getState().searchTabDocId).toBe('docA');

    // SYNCHRONOUS doc-change shape (pendingReference fast-path): a prefetch commit for
    // a DIFFERENT doc than the store shows is dropped as stale (INVARIANT(doc-desync)),
    // so the doc change is driven the way an in-app cross-doc ref-link click does it.
    useAppStore.getState().setPendingReference(makeRef({ document_id: 'docB' }));
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'docB' }));
    expect(useUIStore.getState().searchTabDocId).toBeNull();
  });

  it('keeps the search-tab overlay on a same-doc re-commit', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'docA' }) });
    useUIStore.getState().setRightPanelTab('docA', 'search');
    useAppStore.getState().setCurrentDocument(
      makeDoc({ document_id: 'docA' }),
      { references: [], restoredReference: null },
    );
    expect(useUIStore.getState().searchTabDocId).toBe('docA');
  });
});

describe('setCurrentDocument — prefetched reference (atomic open)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('commits document + restoredReference + references in a single set, no async fetch', () => {
    const getSpy = vi.spyOn(apiClient, 'get');
    const doc = makeDoc({ document_id: 'doc-1' });
    const ref = makeRef({ reference_id: 'ref-1' });

    // No currentProject — the old fast path would have committed ref-less here.
    useAppStore.getState().setCurrentDocument(doc, {
      references: [ref],
      restoredReference: ref,
    });

    const state = useAppStore.getState();
    expect(state.currentDocument?.document_id).toBe('doc-1');
    expect(state.currentReference?.reference_id).toBe('ref-1');
    expect(state.references).toHaveLength(1);
    // The prefetch path must NOT trigger the deferred /references fetch.
    expect(getSpy).not.toHaveBeenCalled();
  });

  it('commits a null restoredReference (no saved ref) without fetching', () => {
    const getSpy = vi.spyOn(apiClient, 'get');
    const doc = makeDoc({ document_id: 'doc-2' });

    useAppStore.getState().setCurrentDocument(doc, {
      references: [],
      restoredReference: null,
    });

    const state = useAppStore.getState();
    expect(state.currentDocument?.document_id).toBe('doc-2');
    expect(state.currentReference).toBeNull();
    expect(getSpy).not.toHaveBeenCalled();
  });

  it('emits exactly one store notification for the prefetched commit', () => {
    const doc = makeDoc({ document_id: 'doc-3' });
    const ref = makeRef({ reference_id: 'ref-3' });
    let notifications = 0;
    const unsub = useAppStore.subscribe(() => { notifications += 1; });
    useAppStore.getState().setCurrentDocument(doc, {
      references: [ref],
      restoredReference: ref,
    });
    unsub();
    expect(notifications).toBe(1);
  });

  it('an id-only restore stub (panel quick preview, cross-doc ref) commits the document alone and promotes the ref via the id-only hydrate', async () => {
    // The stub carries only reference_id — committing it as currentReference would
    // render a title-less reference (RefPanelPlaque crashed on it live). The document
    // commits with no reference; the id-only GET then promotes the full reference.
    clearHydrateInFlight();
    const stub = { reference_id: 'ref-foreign' } as Reference;
    const full = makeRef({ reference_id: 'ref-foreign', document_id: 'doc-other', content: '# foreign', has_content: true });
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue(full);

    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-1' }), {
      references: [],
      restoredReference: stub,
    });
    // Synchronous commit: no stub in the store.
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(getSpy.mock.calls.map(c => c[0])).toContain('/references/ref-foreign');

    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    const cur = useAppStore.getState().currentReference;
    expect(cur?.reference_id).toBe('ref-foreign');
    expect(cur?.title).toBe(full.title);
  });

  it('an id-only restore stub whose reference was deleted clears the stale pointer (no re-fetch on the next open)', async () => {
    clearHydrateInFlight();
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-gone');
    vi.spyOn(apiClient, 'get').mockRejectedValue(new Error('404'));
    vi.spyOn(console, 'error').mockImplementation(() => {});

    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-1' }), {
      references: [],
      restoredReference: { reference_id: 'ref-gone' } as Reference,
    });
    await new Promise(r => setTimeout(r, 0));
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-1')).toBeNull();
  });

  it('a pending cross-doc reference wins the commit and the post-commit hydrate targets the winner (not restoredReference)', async () => {
    // Regression (root cause 1 — ref-link navigation revert): a cross-doc ref-link
    // click sets pendingReference and navigates; setCurrentDocument then commits with
    // the PERSISTED restoredReference in the prefetch. applyRef (the pending) must win
    // currentReference, AND the post-commit body hydrate must target the WINNER — never
    // restoredReference blindly. The old flow hydrated restoredReference from
    // DocumentPage, and hydrateReference's unguarded line-615 marker set yanked
    // currentReference off the clicked target (R2 flashed, then the persisted R5 took
    // over). setCurrentDocument now owns the hydrate of finalRef = applyRef ?? restoredRef.
    clearHydrateInFlight();
    // Persist a DIFFERENT ref for D2 — the one the buggy hydrate used to promote.
    useUIStore.getState().setCurrentReferenceForDoc('doc-2', 'ref-R5');

    // The clicked target. Content-less in-store so the winner-hydrate fires a GET
    // we can positively assert (a hydrated R2 would short-circuit and hide the signal).
    // document_id = D2 so applyRef matches the navigation target.
    const r2 = makeRef({
      reference_id: 'ref-R2', document_id: 'doc-2',
      content: undefined, has_content: true,
    });
    // The persisted ref, incoming as a content-less list-row via prefetch.
    const r5List = makeRef({
      reference_id: 'ref-R5', document_id: 'doc-2',
      content: undefined, has_content: true,
    });
    useAppStore.getState().setReferences([r2]);

    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue(
      makeRef({ reference_id: 'ref-R2', document_id: 'doc-2', content: '# R2 body', has_content: true }),
    );

    // Pending winner = R2 (the cross-doc link handler set this before navigating).
    useAppStore.getState().setPendingReference(r2);

    const doc = makeDoc({ document_id: 'doc-2' });
    useAppStore.getState().setCurrentDocument(doc, {
      references: [r5List],
      restoredReference: r5List,
    });

    // applyRef (pending R2) won currentReference — NOT the persisted R5.
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-R2');
    // The post-commit hydrate targets the WINNER, never restoredReference.
    const fetchedIds = getSpy.mock.calls
      .map((c) => (c[0] as string).match(/\/references\/([^/?]+)$/)?.[1])
      .filter(Boolean) as string[];
    expect(fetchedIds).toContain('ref-R2');
    expect(fetchedIds).not.toContain('ref-R5');

    // Drain the fire-and-forget hydrate so it cannot leak into the next test.
    await new Promise((r) => setTimeout(r, 0));
    vi.restoreAllMocks();
  });
});

describe('setCurrentDocument — restoreFlow (non-prefetch) hydrates the restored ref', () => {
  // Locks the risk that the winner-hydrate inside commit() must
  // cover the restoreFlow path too (non-prefetch in-app doc open via DocumentTree /
  // Header). Previously restoreFlow had its own `if (restored) void hydrateReference(restored)`
  // line; that was removed when commit() became the single owner. This test pins the
  // equivalence: restoreFlow's commit hydrates finalRef (=== restoredRef in this path,
  // since applyRef is null — no pendingReference) so a persisted content-less ref still
  // gets its body fetched. If someone later drops the winner-hydrate from commit(), this
  // goes red.
  beforeEach(() => {
    clearHydrateInFlight();
  });

  it('restoreFlow hydrates the persisted restored reference (content-less list row → GET fires)', async () => {
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-1' }), pendingReference: null });
    // Persist R5 for doc-2 — the ref restoreFlow will resolve and commit.
    useUIStore.getState().setCurrentReferenceForDoc('doc-2', 'ref-R5');

    // R5 arrives as a content-less list row (the LIST endpoint is metadata-only).
    const r5List = makeRef({
      reference_id: 'ref-R5', document_id: 'doc-2',
      content: undefined, has_content: true,
    });
    // Mock the panel/restore list fetch — restoreFlow calls loadReferences(project, doc).
    const loadSpy = vi.spyOn(refsFetchNS, 'loadReferences').mockResolvedValue([r5List]);
    // Mock the single-ref body hydrate — commit's winner-hydrate fires GET /references/R5.
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue(
      makeRef({ reference_id: 'ref-R5', document_id: 'doc-2', content: '# R5 body', has_content: true }),
    );

    const doc = makeDoc({ document_id: 'doc-2' });
    // NO prefetch → setCurrentDocument enters restoreFlow (doc + currentProject + no applyRef).
    useAppStore.getState().setCurrentDocument(doc);

    // restoreFlow is fire-and-forget; flush its await chain (awaitProjectPrefs resolves
    // immediately — no promise registered — then the loadReferences mock).
    await new Promise((r) => setTimeout(r, 0));
    // Drain the commit's fire-and-forget winner-hydrate.
    await new Promise((r) => setTimeout(r, 0));

    // The persisted restored ref won (no pendingReference in this path).
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-R5');
    // restoreFlow fetched the scoped list.
    expect(loadSpy).toHaveBeenCalledWith('proj-1', 'doc-2');
    // THE GUARD: the winner-hydrate fired for the restored ref (the body GET). If the
    // hydrate is dropped from commit(), this fails — the persisted ref would render as a
    // perpetual loading state (content-less commit, never hydrated).
    const fetchedIds = getSpy.mock.calls
      .map((c) => (c[0] as string).match(/\/references\/([^/?]+)$/)?.[1])
      .filter(Boolean) as string[];
    expect(fetchedIds).toContain('ref-R5');

    await new Promise((r) => setTimeout(r, 0));
    vi.restoreAllMocks();
  });

  it('restoreFlow commits ref-less when no ref is persisted for the doc (no hydrate)', async () => {
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-1' }), pendingReference: null });
    // No setCurrentReferenceForDoc → resolveRestoredReference returns null.
    vi.spyOn(refsFetchNS, 'loadReferences').mockResolvedValue([]);
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue({} as never);

    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-empty' }));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(useAppStore.getState().currentReference).toBeNull();
    // No body hydrate fires when there is no restored ref.
    const refGets = getSpy.mock.calls.some((c) => /\/references\/[^/?]+$/.test(c[0] as string));
    expect(refGets).toBe(false);

    vi.restoreAllMocks();
  });

  it('restoreFlow toasts failedToLoadReferences when the refs fetch rejects (doc still commits)', async () => {
    // T2 (tech-debt audit): a rejected loadReferences during doc-switch was swallowed
    // with a bare commit(null) — the doc opened ref-less with no feedback (contrast
    // hydrateReference which toasts failedToFetchReference). The fallback stays, but
    // the user must be told the refs failed to load.
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-1' }), pendingReference: null });
    // A persisted ref id forces restoreFlow into the try-block loadReferences path.
    useUIStore.getState().setCurrentReferenceForDoc('doc-2', 'ref-R5');
    vi.spyOn(refsFetchNS, 'loadReferences').mockRejectedValue(new Error('network'));
    const toast = vi.fn();
    useAppStore.setState({ showToast: toast });

    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-2' }));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    // Graceful fallback preserved: doc still commits ref-less.
    expect(useAppStore.getState().currentReference).toBeNull();
    // No silent degradation — user is told.
    expect(toast).toHaveBeenCalledWith(t('failedToLoadReferences'), 'error');

    vi.restoreAllMocks();
  });
});

describe('setCurrentDocument — restoreFlow stale-commit guard (doc-switch desync)', () => {
  // Repro of the live desync (2026-08-25 gray drive): rapid Beta↔Alpha tree switching
  // left URL=/docs/alpha while the store still named Beta + its restored reference.
  // Header's click handler starts restoreFlow for Beta (non-prefetch); the URL-driven
  // DocumentPage effect then commits Alpha with prefetch; Beta's late flow landed AFTER
  // it — its only guard was project identity, which a same-project doc switch never
  // trips. The fix: an epoch + doc-identity guard on the flow's commits (a commit for
  // doc X is dropped when a NEWER open already committed a different doc).
  beforeEach(() => {
    clearHydrateInFlight();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('drops a late restoreFlow commit when a newer doc already committed (URL moved on)', async () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-alpha' }),
      pendingReference: null,
    });
    // Beta has a persisted reference pointer — restoreFlow resolves it from the LIST.
    useUIStore.getState().setCurrentReferenceForDoc('doc-beta', 'ref-beta');
    const betaRefList = makeRef({
      reference_id: 'ref-beta', document_id: 'doc-beta',
      content: undefined, has_content: true,
    });
    // Beta's scoped LIST GET is manual (parks restoreFlow); any other doc resolves empty.
    let resolveBeta!: (refs: Reference[]) => void;
    vi.spyOn(refsFetchNS, 'loadReferences').mockImplementation((_pid: string, docId: string) =>
      docId === 'doc-beta'
        ? new Promise((r) => { resolveBeta = r as never; })
        : Promise.resolve([]),
    );
    vi.spyOn(apiClient, 'get').mockResolvedValue(
      makeRef({ reference_id: 'ref-beta', content: '# beta body', has_content: true }),
    );

    // 1) Header click on Beta: restoreFlow starts and parks on the LIST GET.
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-beta' }));
    await new Promise((r) => setTimeout(r, 0));

    // 2) URL-driven open of Alpha commits (DocumentPage prefetch path).
    useAppStore.getState().setCurrentDocument(
      makeDoc({ document_id: 'doc-alpha' }),
      { references: [], restoredReference: null },
    );
    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-alpha');

    // 3) Beta's LIST GET resolves LAST — the stale commit must be dropped.
    resolveBeta([betaRefList]);
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-alpha');
    expect(useAppStore.getState().currentReference).toBeNull();
  });

  it('drops a late commit(null) from the flow parked on prefs when a newer doc committed', async () => {
    // Same desync parked one stage earlier: the flow awaits project prefs, the user
    // switches docs while it waits, and the late ref-less commit(null) (no-saved-ref
    // branch) must not resurrect the abandoned doc.
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-alpha' }),
      pendingReference: null,
    });
    // No setCurrentReferenceForDoc for doc-beta → the no-saved-ref branch runs.
    let resolvePrefs!: (v: unknown) => void;
    vi.spyOn(apiClient, 'get').mockImplementation((endpoint: string) =>
      endpoint.startsWith('/preferences/')
        ? new Promise((r) => { resolvePrefs = r; })
        : Promise.resolve({} as never),
    );
    // Seed the in-flight prefs promise the flow parks on.
    useUIStore.getState().loadProjectPrefs('user-1', 'proj-1');

    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-beta' }));
    // URL-driven open of Alpha commits while Beta's flow is parked on prefs.
    useAppStore.getState().setCurrentDocument(
      makeDoc({ document_id: 'doc-alpha' }),
      { references: [], restoredReference: null },
    );

    resolvePrefs({ documents: {} });
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-alpha');
    expect(useAppStore.getState().currentReference).toBeNull();
  });

  it('drops a stale URL-driven prefetch commit when a newer doc already committed', () => {
    // The second stale writer (measured on gray 2026-08-25): the DocumentPage effect's
    // fetch continuation can run BEFORE React flips its `cancelled` cleanup flag, so
    // its setCurrentDocument(doc, prefetch) lands after the user already navigated to
    // another doc. A prefetch for a doc the store does NOT show (and is not null) is
    // an abandoned navigation — it must be dropped, or the store rewinds behind the URL.
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-alpha' }),
      pendingReference: null,
    });

    // Beta's LATE effect call (its navigation was already replaced by Alpha's).
    useAppStore.getState().setCurrentDocument(
      makeDoc({ document_id: 'doc-beta' }),
      { references: [], restoredReference: null },
    );

    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-alpha');
  });

  it('still commits a late flow when the store is on the SAME doc (guard keys on identity, not lateness)', async () => {
    // The legit late-commit case a blanket guard would break: a newer intent committed
    // the SAME doc (epoch advanced), and the in-app flow resolving after it is a
    // same-doc refresh — it must land AND keep an open preview (same-doc metadata
    // refresh semantics, see setCurrentDocument WHY note).
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-alpha' }),
      pendingReference: null,
    });
    useUIStore.getState().setCurrentReferenceForDoc('doc-beta', 'ref-beta');
    const betaRefList = makeRef({
      reference_id: 'ref-beta', document_id: 'doc-beta',
      content: '# body', has_content: true,
    });
    let resolveBeta!: (refs: Reference[]) => void;
    vi.spyOn(refsFetchNS, 'loadReferences').mockImplementation(() =>
      new Promise((r) => { resolveBeta = r as never; }),
    );

    // Header click on Beta starts the flow and parks on the LIST GET…
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-beta' }));
    await new Promise((r) => setTimeout(r, 0));
    // …a newer intent puts Beta itself on screen (e.g. the pendingReference fast path
    // of a second Beta click), which the URL-driven prefetch then refreshes in place.
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-beta' }) });
    useAppStore.getState().setCurrentDocument(
      makeDoc({ document_id: 'doc-beta' }),
      { references: [betaRefList], restoredReference: betaRefList },
    );
    useAppStore.getState().setPreviewDocument(makeDoc({ document_id: 'doc-omega' }));

    // The flow resolves AFTER the newer same-doc commits — identity match, so it lands.
    resolveBeta([betaRefList]);
    await new Promise((r) => setTimeout(r, 0));

    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-beta');
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-beta');
    // Same-doc refresh semantics preserved: the open preview survives the late commit.
    expect(useAppStore.getState().previewDocument?.document_id).toBe('doc-omega');
  });
});

describe('setActiveNoteThreadId', () => {
  it('persists thread id per document in documentNoteThreads', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });
    useNoteStore.getState().setActiveNoteThreadId('thread-A', 'doc-1');
    expect(useNoteStore.getState().documentNoteThreads['doc-1']).toBe('thread-A');
  });

  it('does not persist if no current document', () => {
    useNoteStore.getState().setActiveNoteThreadId('thread-B');
    expect(useNoteStore.getState().activeNoteThreadId).toBe('thread-B');
    expect(Object.keys(useNoteStore.getState().documentNoteThreads)).toHaveLength(0);
  });
});

describe('selectionEmpty', () => {
  it('defaults to true', () => {
    expect(useUIStore.getState().selectionEmpty).toBe(true);
  });

  it('can be set to false when text is selected', () => {
    useUIStore.getState().setSelectionEmpty(false);
    expect(useUIStore.getState().selectionEmpty).toBe(false);
  });

  it('can be reset to true when selection collapses', () => {
    useUIStore.getState().setSelectionEmpty(false);
    useUIStore.getState().setSelectionEmpty(true);
    expect(useUIStore.getState().selectionEmpty).toBe(true);
  });
});

// ── Granular reference actions ───────────────────────────────────────────────

describe('addReference', () => {
  it('prepends a new reference', () => {
    const existing = makeRef({ reference_id: 'ref-1' });
    useAppStore.getState().setReferences([existing]);
    const newRef = makeRef({ reference_id: 'ref-2', title: 'New' });
    useAppStore.getState().addReference(newRef);
    const refs = useAppStore.getState().references;
    expect(refs).toHaveLength(2);
    expect(refs[0].reference_id).toBe('ref-2');
    expect(refs[1].reference_id).toBe('ref-1');
  });

  it('deduplicates by reference_id', () => {
    const existing = makeRef({ reference_id: 'ref-1', title: 'Old' });
    useAppStore.getState().setReferences([existing]);
    const dupe = makeRef({ reference_id: 'ref-1', title: 'Updated' });
    useAppStore.getState().addReference(dupe);
    const refs = useAppStore.getState().references;
    expect(refs).toHaveLength(1);
    expect(refs[0].title).toBe('Updated');
  });
});

describe('removeReference', () => {
  it('removes a reference by id', () => {
    useAppStore.getState().setReferences([
      makeRef({ reference_id: 'ref-1' }),
      makeRef({ reference_id: 'ref-2' }),
    ]);
    useAppStore.getState().removeReference('ref-1');
    const refs = useAppStore.getState().references;
    expect(refs).toHaveLength(1);
    expect(refs[0].reference_id).toBe('ref-2');
  });

  it('does nothing if id not found', () => {
    useAppStore.getState().setReferences([makeRef({ reference_id: 'ref-1' })]);
    useAppStore.getState().removeReference('nonexistent');
    expect(useAppStore.getState().references).toHaveLength(1);
  });

  it('mergeReferences filters out IDs currently in deletedRefIds', () => {
    useAppStore.setState({ deletedRefIds: new Set<string>() });
    useAppStore.getState().addRefToDeleting('ref-1');
    useAppStore.getState().addRefToDeleting('ref-3');
    useAppStore.getState().mergeReferences([
      makeRef({ reference_id: 'ref-1' }),
      makeRef({ reference_id: 'ref-2' }),
      makeRef({ reference_id: 'ref-3' }),
    ]);
    const ids = useAppStore.getState().references.map(r => r.reference_id);
    expect(ids).toEqual(['ref-2']);
  });

  it('setReferences ignores deletedRefIds (rollback/save flow must not be filtered)', () => {
    useAppStore.setState({ deletedRefIds: new Set<string>() });
    useAppStore.getState().addRefToDeleting('ref-1');
    useAppStore.getState().setReferences([
      makeRef({ reference_id: 'ref-1' }),
      makeRef({ reference_id: 'ref-2' }),
    ]);
    expect(useAppStore.getState().references).toHaveLength(2);
  });

  it('addReference clears the ref from deletedRefIds (undo rollback)', () => {
    useAppStore.setState({ deletedRefIds: new Set<string>() });
    useAppStore.getState().addRefToDeleting('ref-1');
    expect(useAppStore.getState().deletedRefIds.has('ref-1')).toBe(true);
    useAppStore.getState().addReference(makeRef({ reference_id: 'ref-1' }));
    expect(useAppStore.getState().deletedRefIds.has('ref-1')).toBe(false);
  });

  it('addReference can undo a single removal without affecting others (no cascading rollback)', () => {
    const refs = [
      makeRef({ reference_id: 'ref-1' }),
      makeRef({ reference_id: 'ref-2' }),
      makeRef({ reference_id: 'ref-3' }),
      makeRef({ reference_id: 'ref-4' }),
      makeRef({ reference_id: 'ref-5' }),
    ];
    useAppStore.getState().setReferences(refs);

    useAppStore.getState().removeReference('ref-1');
    useAppStore.getState().removeReference('ref-2');
    useAppStore.getState().removeReference('ref-3');
    expect(useAppStore.getState().references).toHaveLength(2);

    useAppStore.getState().addReference(refs[1]);

    const result = useAppStore.getState().references;
    expect(result).toHaveLength(3);
    const ids = result.map(r => r.reference_id).sort();
    expect(ids).toEqual(['ref-2', 'ref-4', 'ref-5']);
  });
});

describe('updateReference', () => {
  it('shallow-merges patch into matching reference', () => {
    useAppStore.getState().setReferences([
      makeRef({ reference_id: 'ref-1', title: 'Old', content: 'text' }),
    ]);
    useAppStore.getState().updateReference('ref-1', { title: 'New' });
    const ref = useAppStore.getState().references[0];
    expect(ref.title).toBe('New');
    expect(ref.content).toBe('text');
  });

  it('does nothing if id not found', () => {
    useAppStore.getState().setReferences([makeRef({ reference_id: 'ref-1', title: 'Old' })]);
    useAppStore.getState().updateReference('nonexistent', { title: 'New' });
    expect(useAppStore.getState().references[0].title).toBe('Old');
  });
});

describe('replaceReference', () => {
  it('swaps temp reference with real one (optimistic UI)', () => {
    const temp = makeRef({ reference_id: 'temp_123', title: 'Uploading...' });
    const real = makeRef({ reference_id: 'ref-real', title: 'Done' });
    useAppStore.getState().setReferences([temp, makeRef({ reference_id: 'ref-other' })]);
    useAppStore.getState().replaceReference('temp_123', real);
    const refs = useAppStore.getState().references;
    expect(refs).toHaveLength(2);
    expect(refs[0].reference_id).toBe('ref-real');
    expect(refs[0].title).toBe('Done');
    expect(refs[1].reference_id).toBe('ref-other');
  });

  it('deduplicates if real ref already exists (WS race)', () => {
    const temp = makeRef({ reference_id: 'temp_123' });
    const real = makeRef({ reference_id: 'ref-real', title: 'From WS' });
    useAppStore.getState().setReferences([temp, real]);
    const replacement = makeRef({ reference_id: 'ref-real', title: 'From API' });
    useAppStore.getState().replaceReference('temp_123', replacement);
    const refs = useAppStore.getState().references;
    expect(refs).toHaveLength(1);
    expect(refs[0].reference_id).toBe('ref-real');
  });
});

// ── mergeReferences — field-preserving merge (content lazy-fetched) ──────────

describe('mergeReferences (metadata-only list + hydrated content)', () => {
  it('preserves hydrated content+headings on a content-less has_content:true refetch', () => {
    // The store holds a fully-hydrated ref (body fetched via GET /references/{id}).
    const hydrated = makeRef({
      reference_id: 'ref-1',
      content: '# Body',
      headings: [{ level: 1, text: 'Body', line: 0 }],
      has_content: true,
    });
    useAppStore.getState().setReferences([hydrated]);
    // A panel refetch returns the metadata-only server row (no content, has_content true),
    // with a bumped updated_at.
    const metaOnly = {
      ...hydrated,
      content: undefined,
      headings: undefined,
      updated_at: '2024-02-02T00:00:00Z',
    };
    useAppStore.getState().mergeReferences([metaOnly]);
    const ref = useAppStore.getState().references[0];
    // Hydrated content + headings survive the metadata-only refetch.
    expect(ref.content).toBe('# Body');
    expect(ref.headings?.[0]?.text).toBe('Body');
    // Metadata still refreshes.
    expect(ref.updated_at).toBe('2024-02-02T00:00:00Z');
  });

  it('overwrites content to "" when has_content is false (genuinely cleared ref)', () => {
    const hydrated = makeRef({
      reference_id: 'ref-1', content: '# Body', has_content: true,
    });
    useAppStore.getState().setReferences([hydrated]);
    // Server now reports the ref as empty — keeping the old body would show stale
    // content as current (no-silent-degradation violation).
    const cleared = { ...hydrated, content: undefined, has_content: false };
    useAppStore.getState().mergeReferences([cleared]);
    const ref = useAppStore.getState().references[0];
    expect(ref.content).toBe('');
  });

  it('takes the server row verbatim when no prior hydrated state exists', () => {
    const metaOnly = makeRef({ reference_id: 'ref-new', content: undefined, has_content: true });
    useAppStore.getState().mergeReferences([metaOnly]);
    const ref = useAppStore.getState().references[0];
    expect(ref.content).toBeUndefined();
    expect(ref.has_content).toBe(true);
  });

  it('a doc-switch commit (setCurrentDocument prefetch) preserves a hydrated shared ref', async () => {
    // Regression: the doc-switch commit used to raw-set the content-less list, wiping
    // hydrated bodies of refs shared across docs (index/ancestor refs are in every scope).
    // It now routes through the field-preserving merge — atomically.
    const indexRefHydrated = makeRef({
      reference_id: 'idx-ref', content: '# Index body', headings: [{ level: 1, text: 'Index body', line: 0 }],
      has_content: true,
    });
    useAppStore.getState().setReferences([indexRefHydrated]);

    const doc = makeDoc({ document_id: 'doc-2' });
    // The new doc's LIST returns the same index ref, content-less (metadata-only).
    const metaOnly = { ...indexRefHydrated, content: undefined, headings: undefined, updated_at: '2024-03-03T00:00:00Z' };
    useAppStore.getState().setCurrentDocument(doc, { references: [metaOnly], restoredReference: null });

    const ref = useAppStore.getState().references[0];
    // Hydrated body survives the doc-switch commit (no loading flicker / re-fetch).
    expect(ref.content).toBe('# Index body');
    expect(ref.headings?.[0]?.text).toBe('Index body');
    expect(ref.updated_at).toBe('2024-03-03T00:00:00Z');
  });

  it('a same-doc re-commit keeps the restored reference hydrated (no loading blink)', () => {
    // An in-app doc open commits TWICE (Header's optimistic open, then DocumentPage's
    // prefetch). The first commit's hydrate fills the body; the second hands in the same
    // restored ref as a content-less list row. Committing that raw row as
    // currentReference swapped the open quick-preview editor back to "Loading…".
    const doc = makeDoc({ document_id: 'doc-1' });
    const hydrated = makeRef({ reference_id: 'ref-1', content: '# Body', has_content: true });
    useAppStore.setState({ currentDocument: doc, currentReference: hydrated, references: [hydrated] });

    const metaOnly = { ...hydrated, content: undefined, headings: undefined };
    useAppStore.getState().setCurrentDocument(doc, { references: [metaOnly], restoredReference: metaOnly });

    expect(useAppStore.getState().currentReference?.content).toBe('# Body');
  });

  it('returning to a doc restores a body that left the list with its scope (same updated_at)', () => {
    // A ref owned by doc A only: switching to B drops it from the list (B's scope does
    // not carry it). Returning to A must commit it hydrated — no "Loading…" + refetch.
    const docA = makeDoc({ document_id: 'doc-A' });
    const docB = makeDoc({ document_id: 'doc-B' });
    const hydrated = makeRef({ reference_id: 'ref-own', content: '# Own', has_content: true, updated_at: '2024-05-05T00:00:00Z' });
    useAppStore.setState({ currentDocument: docA, currentReference: hydrated, references: [hydrated] });

    // In-app switch: Header's optimistic open, then DocumentPage's prefetch of B.
    useAppStore.getState().setCurrentDocument(docB);
    useAppStore.getState().setCurrentDocument(docB, { references: [], restoredReference: null });
    expect(useAppStore.getState().references).toHaveLength(0);

    const metaOnly = { ...hydrated, content: undefined, headings: undefined };
    useAppStore.getState().setCurrentDocument(docA);
    useAppStore.getState().setCurrentDocument(docA, { references: [metaOnly], restoredReference: metaOnly });

    expect(useAppStore.getState().currentReference?.content).toBe('# Own');
    expect(useAppStore.getState().references[0].content).toBe('# Own');
  });

  it('a body that left the list is NOT restored once the ref changed meanwhile (updated_at moved)', () => {
    // Another user edited the ref while we were on doc B — every save bumps updated_at.
    // The stashed body is stale: it must not be shown as current.
    const docA = makeDoc({ document_id: 'doc-A' });
    const docB = makeDoc({ document_id: 'doc-B' });
    const hydrated = makeRef({ reference_id: 'ref-edited', content: '# Old', has_content: true, updated_at: '2024-05-05T00:00:00Z' });
    useAppStore.setState({ currentDocument: docA, currentReference: hydrated, references: [hydrated] });

    // In-app switch: Header's optimistic open, then DocumentPage's prefetch of B.
    useAppStore.getState().setCurrentDocument(docB);
    useAppStore.getState().setCurrentDocument(docB, { references: [], restoredReference: null });

    const metaOnly = { ...hydrated, content: undefined, headings: undefined, updated_at: '2024-06-06T00:00:00Z' };
    vi.spyOn(apiClient, 'get').mockReturnValue(new Promise(() => {}));
    useAppStore.getState().setCurrentDocument(docA);
    useAppStore.getState().setCurrentDocument(docA, { references: [metaOnly], restoredReference: metaOnly });

    expect(useAppStore.getState().currentReference?.content).toBeUndefined();
    vi.restoreAllMocks();
  });

  it('hydrateReference does NOT snap currentReference back if the user navigated away mid-fetch', async () => {
    // Regression: the post-fetch setCurrentReference ran unconditionally, yanking
    // currentReference back to the hydrating ref if the user clicked another during the GET.
    vi.spyOn(apiClient, 'get').mockImplementation((endpoint: string) =>
      endpoint === '/references/ref-A'
        ? Promise.resolve(makeRef({ reference_id: 'ref-A', content: 'A body', has_content: true }))
        : Promise.resolve(makeRef({ reference_id: 'ref-B', content: 'B body', has_content: true })),
    );
    const a = makeRef({ reference_id: 'ref-A', content: undefined, has_content: true });
    useAppStore.getState().setReferences([a]);

    // Start hydrating A (sets currentReference=A loading), but do NOT await yet.
    const hydrateA = useAppStore.getState().hydrateReference(a);
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-A');
    // User clicks B during A's fetch → currentReference becomes B.
    await useAppStore.getState().hydrateReference('ref-B');
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-B');
    // A's fetch resolves — it must NOT snap currentReference back to A.
    await hydrateA;
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-B');
    // A's list entry is still refreshed (replaceReference is unconditional).
    expect(useAppStore.getState().references.find(r => r.reference_id === 'ref-A')?.content).toBe('A body');
    vi.restoreAllMocks();
  });

  it('hydrateReference collapses concurrent calls for the same id to ONE GET', async () => {
    // Dedup (hydrate-dedup finding): two near-simultaneous opens of the same ref fire
    // a single GET /references/{id} — the second call shares the first's in-flight promise.
    clearHydrateInFlight();
    const getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue(
      makeRef({ reference_id: 'ref-D', content: 'D body', has_content: true }),
    );
    const a = makeRef({ reference_id: 'ref-D', content: undefined, has_content: true });
    useAppStore.getState().setReferences([a]);

    const p1 = useAppStore.getState().hydrateReference(a);
    const p2 = useAppStore.getState().hydrateReference(a);
    const [r1, r2] = await Promise.all([p1, p2]);

    // One round-trip, both callers resolved to the same body.
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(r1?.content).toBe('D body');
    expect(r2?.content).toBe('D body');
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-D');
    vi.restoreAllMocks();
  });

  it('an id-only hydrate (ref not in store) does NOT yank currentReference back if superseded', async () => {
    // Regression for the id-only path (finding id-only-yank): previously it promoted
    // unconditionally on resolve. Now the latest-wins guard (hydrateLastId) covers it
    // uniformly — a late-settling id-only fetch must not snap the selection back.
    clearHydrateInFlight();
    vi.spyOn(apiClient, 'get').mockImplementation((endpoint: string) =>
      endpoint === '/references/cross-A'
        ? Promise.resolve(makeRef({ reference_id: 'cross-A', content: 'A body', has_content: true }))
        : Promise.resolve(makeRef({ reference_id: 'cross-B', content: 'B body', has_content: true })),
    );
    // Neither ref is in the store → both go through the id-only path (no loading marker).
    useAppStore.getState().setReferences([]);

    const hydrateA = useAppStore.getState().hydrateReference('cross-A');
    // Before A resolves the user opens B; B is the latest request.
    await useAppStore.getState().hydrateReference('cross-B');
    expect(useAppStore.getState().currentReference?.reference_id).toBe('cross-B');
    // A settles late — it must NOT yank currentReference back to cross-A.
    await hydrateA;
    expect(useAppStore.getState().currentReference?.reference_id).toBe('cross-B');
    vi.restoreAllMocks();
  });

  it('clearHydrateInFlight (project switch) blocks a late resolve from promoting', async () => {
    // Cross-project guard: a hydrate GET started in project P, the user switches project
    // (clearHydrateInFlight runs), then the GET resolves — it must not commit the foreign
    // ref into the new project's store/selection.
    clearHydrateInFlight();
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-P' }) });
    let resolveA!: (v: Reference) => void;
    vi.spyOn(apiClient, 'get').mockReturnValueOnce(new Promise((r) => { resolveA = r as never; }));
    useAppStore.getState().setReferences([]);

    const hydrateA = useAppStore.getState().hydrateReference('ref-X');
    // Project switch fires (clears in-flight + hydrateLastId) and moves to a new project.
    clearHydrateInFlight();
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-Q' }) });
    useAppStore.getState().setCurrentReference(null);

    resolveA(makeRef({ reference_id: 'ref-X', content: 'stale', has_content: true }));
    await hydrateA;

    // The foreign ref was NOT promoted into project Q, and NOT added to its list.
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(useAppStore.getState().references.find(r => r.reference_id === 'ref-X')).toBeUndefined();
    useAppStore.setState({ currentProject: null });
    vi.restoreAllMocks();
  });

  it('a late in-store hydrate does NOT yank currentReference back after a non-hydrate navigation', async () => {
    // Regression for the latest-wins rewrite: hydrateLastId is only bumped inside
    // hydrateReference, so non-hydrate selection changes (back button → setCurrentReference
    // null, doc open, collab null, chat source) are invisible to it. The in-store path
    // must ALSO observe the actual currentReference, or a late-settling hydrate yanks the
    // selection back to the ref the user already left.
    clearHydrateInFlight();
    let resolveA!: (v: Reference) => void;
    vi.spyOn(apiClient, 'get').mockReturnValueOnce(new Promise((r) => { resolveA = r as never; }));
    const a = makeRef({ reference_id: 'ref-A', content: undefined, has_content: true });
    useAppStore.getState().setReferences([a]);

    // Start hydrating A (marker sets currentReference=A); do NOT await.
    const hydrateA = useAppStore.getState().hydrateReference(a);
    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-A');
    // User navigates away via a NON-hydrate path (back button / doc open): currentReference
    // is cleared directly. hydrateLastId stays 'ref-A' (it's only bumped by hydrateReference).
    useAppStore.getState().setCurrentReference(null);
    // A's GET resolves late — it must NOT yank currentReference back to ref-A.
    resolveA(makeRef({ reference_id: 'ref-A', content: 'A body', has_content: true }));
    await hydrateA;
    expect(useAppStore.getState().currentReference).toBeNull();
    // The list entry is still refreshed (updateReference runs regardless of selection).
    expect(useAppStore.getState().references.find(r => r.reference_id === 'ref-A')?.content).toBe('A body');
    vi.restoreAllMocks();
  });

  it('hydrateReference refreshes an in-store ref IN PLACE (no panel reorder)', async () => {
    // replaceReference would prepend the hydrated ref (full rebuild + reorder churn on
    // every hydrate); the in-store path now uses updateReference to preserve list position.
    clearHydrateInFlight();
    vi.spyOn(apiClient, 'get').mockResolvedValue(
      makeRef({ reference_id: 'ref-mid', content: 'body', has_content: true }),
    );
    const a = makeRef({ reference_id: 'ref-first', content: 'x', has_content: true });
    const b = makeRef({ reference_id: 'ref-mid', content: undefined, has_content: true });
    const c = makeRef({ reference_id: 'ref-last', content: 'y', has_content: true });
    useAppStore.getState().setReferences([a, b, c]);

    await useAppStore.getState().hydrateReference(b);

    const ids = useAppStore.getState().references.map(r => r.reference_id);
    // Position preserved — ref-mid stays in the middle, not prepended to index 0.
    expect(ids).toEqual(['ref-first', 'ref-mid', 'ref-last']);
    expect(useAppStore.getState().references[1].content).toBe('body');
    vi.restoreAllMocks();
  });

  it('cold F5: project undefined at fetch start, populated before resolve, still promotes the ref', async () => {
    // Regression: the guard bailed when currentProject went undefined → real id mid-GET,
    // mistaking cold-load project population for a project switch and stranding the ref
    // on "Loading…" forever. A start value of undefined must match any later id.
    clearHydrateInFlight();
    useAppStore.setState({ currentProject: null });
    let resolveA!: (v: Reference) => void;
    vi.spyOn(apiClient, 'get').mockReturnValueOnce(new Promise((r) => { resolveA = r as never; }));
    const a = makeRef({ reference_id: 'ref-cold', content: undefined, has_content: true });
    useAppStore.getState().setReferences([a]);

    const hydrateA = useAppStore.getState().hydrateReference(a);
    // Project loads while the GET is in flight (GET /projects resolves after mount).
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-P' }) });
    resolveA(makeRef({ reference_id: 'ref-cold', content: 'cold body', has_content: true }));
    await hydrateA;

    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-cold');
    expect(useAppStore.getState().currentReference?.content).toBe('cold body');
    useAppStore.setState({ currentProject: null });
    vi.restoreAllMocks();
  });

  it('genuine switch (real project A → real project B mid-GET) still bails', async () => {
    clearHydrateInFlight();
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-A' }) });
    let resolveA!: (v: Reference) => void;
    vi.spyOn(apiClient, 'get').mockReturnValueOnce(new Promise((r) => { resolveA = r as never; }));
    const a = makeRef({ reference_id: 'ref-sw', content: undefined, has_content: true });
    useAppStore.getState().setReferences([a]);

    const hydrateA = useAppStore.getState().hydrateReference(a);
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-B' }) });
    useAppStore.getState().setCurrentReference(null);
    resolveA(makeRef({ reference_id: 'ref-sw', content: 'stale', has_content: true }));
    await hydrateA;

    // Foreign ref not promoted into project B.
    expect(useAppStore.getState().currentReference).toBeNull();
    useAppStore.setState({ currentProject: null });
    vi.restoreAllMocks();
  });
});

// ── setCurrentProject — state isolation on project switch ────────────────────

describe('setCurrentProject', () => {
  it('resets accessLevel to readonly on project switch', () => {
    useAppStore.setState({ accessLevel: 'full', currentProject: makeProject({ project_id: 'proj-A' }) });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useAppStore.getState().accessLevel).toBe('readonly');
  });

  it('clears documents and documentTree on project switch', () => {
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-A' }) });
    useAppStore.getState().setDocuments([makeDoc()]);
    expect(useAppStore.getState().documents).toHaveLength(1);
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useAppStore.getState().documents).toHaveLength(0);
    expect(useAppStore.getState().documentTree).toHaveLength(0);
  });

  it('clears references on project switch', () => {
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-A' }) });
    useAppStore.getState().setReferences([makeRef()]);
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useAppStore.getState().references).toHaveLength(0);
  });

  it('clears currentDocument and currentReference on project switch', () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-A' }),
      currentDocument: makeDoc(),
      currentReference: makeRef(),
    });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useAppStore.getState().currentDocument).toBeNull();
    expect(useAppStore.getState().currentReference).toBeNull();
  });

  it('clears activeNoteThreadId on project switch', () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-A' }),
    });
    useNoteStore.setState({ activeNoteThreadId: 'thread-1' });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
  });

  it('clears all collection state when set to null', () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-A' }),
      accessLevel: 'full',
      currentDocument: makeDoc(),
      currentReference: makeRef(),
      documents: [makeDoc()],
      references: [makeRef()],
    });
    useNoteStore.setState({ activeNoteThreadId: 'thread-1' });
    useAppStore.getState().setCurrentProject(null);
    const s = useAppStore.getState();
    expect(s.currentProject).toBeNull();
    expect(s.accessLevel).toBe('readonly');
    expect(s.currentDocument).toBeNull();
    expect(s.currentReference).toBeNull();
    expect(s.documents).toHaveLength(0);
    expect(s.references).toHaveLength(0);
    expect(useNoteStore.getState().activeNoteThreadId).toBeNull();
  });

  it('preserves collections on initial load (prev project is null)', () => {
    useAppStore.setState({ currentProject: null });
    useAppStore.getState().setDocuments([makeDoc()]);
    useAppStore.getState().setReferences([makeRef()]);
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-A' }));
    expect(useAppStore.getState().documents).toHaveLength(1);
    expect(useAppStore.getState().references).toHaveLength(1);
    expect(useAppStore.getState().accessLevel).toBe('full');
  });

  it('preserves collections on same-project re-fetch', () => {
    const proj = makeProject({ project_id: 'proj-A' });
    useAppStore.setState({ currentProject: proj });
    useAppStore.getState().setDocuments([makeDoc()]);
    useAppStore.getState().setReferences([makeRef()]);
    useAppStore.setState({ accessLevel: 'full' });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-A', name: 'Updated' }));
    expect(useAppStore.getState().documents).toHaveLength(1);
    expect(useAppStore.getState().references).toHaveLength(1);
    expect(useAppStore.getState().accessLevel).toBe('full');
    expect(useAppStore.getState().currentProject?.name).toBe('Updated');
  });

  // INVARIANT guard: a throw in one sibling store must not skip the other reset
  // nor abort the project switch (resetSiblingStores error boundary).
  it('still resets ui-store and switches project when note-store reset throws', () => {
    useAppStore.setState({ currentProject: makeProject({ project_id: 'proj-A' }) });
    const toast = vi.fn();
    useAppStore.setState({ showToast: toast });

    const noteSpy = vi.spyOn(useNoteStore.getState(), 'resetForProjectSwitch')
      .mockImplementation(() => { throw new Error('boom'); });
    const uiSpy = vi.spyOn(useUIStore.getState(), 'resetForProjectSwitch');
    vi.spyOn(console, 'error').mockImplementation(() => {});

    expect(() =>
      useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' })),
    ).not.toThrow();

    expect(noteSpy).toHaveBeenCalled();
    expect(uiSpy).toHaveBeenCalled();              // sibling reset still ran
    expect(toast).toHaveBeenCalledWith(expect.stringContaining('notes'), 'error');
    expect(useAppStore.getState().currentProject?.project_id).toBe('proj-B');
  });
});

// ── Persistence: setCurrentUser cleanup ─────────────────────────────────────

function makeUser(overrides: Partial<User> = {}): User {
  return {
    user_id: 'user-1',
    email: 'test@lore.app',
    name: 'Test',
    role: 'user',
    has_pin: false,
    user_facts: '',
    ...overrides,
  };
}

describe('setCurrentUser', () => {
  it('sets user in store', () => {
    useAppStore.getState().setCurrentUser(makeUser());
    expect(useAppStore.getState().currentUser?.user_id).toBe('user-1');
  });

  it('clears user and pinLocked on null', () => {
    useAppStore.getState().setCurrentUser(makeUser());
    useAppStore.setState({ pinLocked: true });
    useAppStore.getState().setCurrentUser(null);
    expect(useAppStore.getState().currentUser).toBeNull();
    expect(useAppStore.getState().pinLocked).toBe(false);
  });

  // INVARIANT: a DIRECT setCurrentUser(null) MUST still clear lastSavedBlobs.
  // Why: it is reachable without going through PinLock/UserControls, and so without
  // resetEditorHost — clearLastSavedBlobs() inside setCurrentUser is the idempotent
  // safety net that keeps a bare logout caller from leaking the prior user's UI blobs.
  it('clears lastSavedBlobs on a direct setCurrentUser(null)', () => {
    const spy = vi.spyOn(uiStoreNS, 'clearLastSavedBlobs');
    useAppStore.getState().setCurrentUser(makeUser());
    useAppStore.getState().setCurrentUser(null);
    expect(spy).toHaveBeenCalled();
    spy.mockRestore();
  });

  // INVARIANT: a soft logout (setCurrentUser(null), no page reload) MUST drop the
  // previous user's SWR caches.
  // Why: a same-tab re-login into a shared project would otherwise flash the prior
  // user's session list (~360ms) before revalidate. The 401 path reloads and is
  // already safe. Each user-scoped cache registers its clear() into the logout
  // registry; setCurrentUser(null) fires it.
  it('clears user-scoped SWR caches on a direct setCurrentUser(null)', () => {
    const registrySpy = vi.spyOn(logoutNS, 'clearUserScopedCaches');
    useAppStore.getState().setCurrentUser(makeUser());
    useAppStore.getState().setCurrentUser(null);
    expect(registrySpy).toHaveBeenCalledTimes(1);
    registrySpy.mockRestore();
  });

  it('does NOT clear caches when setting a user (only on logout)', () => {
    const registrySpy = vi.spyOn(logoutNS, 'clearUserScopedCaches');
    useAppStore.getState().setCurrentUser(makeUser());
    expect(registrySpy).not.toHaveBeenCalled();
    registrySpy.mockRestore();
  });

  it('registers the references SWR cache clear into the logout registry', () => {
    // The references cache lives in api/references-fetch (a leaf app-store already
    // owns), so app-store registers its clear on the cache's behalf. A logout must
    // drop it: verify clearReferencesInFlight is wired into the registry.
    const refsSpy = vi.spyOn(refsFetchNS, 'clearReferencesInFlight');
    useAppStore.getState().setCurrentUser(makeUser());
    useAppStore.getState().setCurrentUser(null);
    expect(refsSpy).toHaveBeenCalledTimes(1);
    refsSpy.mockRestore();
  });

  // INVARIANT: logout must reset the chat/note stores, not only the SWR caches.
  // Why: the store fields (sessions/messages/activeSessionId) are module-level
  // Zustand state that survives a soft logout, and they are what the UI renders — a
  // same-scope re-login would show the prior user's chat. The resets register into
  // the same logout registry.
  it('resets the AI-chat and note-chat store state on a direct setCurrentUser(null)', () => {
    useChatStore.setState({
      sessions: [{ session_id: 'A-secret', title: 'A', mode: 'ask' } as never],
      activeSessionId: 'A-secret',
      messages: [{ message_id: 'm1', content: 'secret' } as never],
      documentId: 'dA',
    });
    useNoteChatStore.setState({
      sessions: [{ session_id: 'A-note', title: 'A note' } as never],
      activeSessionId: 'A-note',
      messages: [{ message_id: 'mn1', content: 'note secret' } as never],
    });

    useAppStore.getState().setCurrentUser(null);

    expect(useChatStore.getState().sessions).toEqual([]);
    expect(useChatStore.getState().activeSessionId).toBeNull();
    expect(useChatStore.getState().messages).toEqual([]);
    expect(useChatStore.getState().documentId).toBeNull();
    expect(useNoteChatStore.getState().sessions).toEqual([]);
    expect(useNoteChatStore.getState().activeSessionId).toBeNull();
    expect(useNoteChatStore.getState().messages).toEqual([]);
  });
});

// ── Persistence: setCurrentProject async prefs fetch ────────────────────────

describe('setCurrentProject — preferences fetch', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fetches server preferences on project switch', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        sidebarTab: 'toc',
        sidebarOpen: false,
        lastActiveChatSessionId: 'saved-chat',
        documents: {
          'd1': {
            mainEntity: { type: 'document', id: 'd1' },
            rightPanelOpen: true,
            rightPanelTab: 'chat',
          },
        },
      }),
    } as Response);

    useAppStore.setState({ currentUser: makeUser(), currentProject: null });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-X' }));

    await vi.waitFor(() => {
      expect(fetchSpy).toHaveBeenCalled();
    });
    await vi.waitFor(() => {
      expect(useUIStore.getState().sidebarTab).toBe('toc');
    });

    expect(useUIStore.getState().documents['d1'].rightPanelTab).toBe('chat');
    expect(useUIStore.getState().getLastActiveChatSession()).toBe('saved-chat');

    fetchSpy.mockRestore();
  });

  it('staleness guard: drops stale fetch result when project changed', async () => {
    const resolvers: Array<(data: unknown) => void> = [];
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      return new Promise(resolve => {
        resolvers.push((data) => resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(data),
        } as Response));
      });
    });

    useAppStore.setState({ currentUser: makeUser(), currentProject: null });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-A' }));

    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));

    resolvers[0]({ sidebarTab: 'toc', documents: { d1: { mainEntity: { type: 'document', id: 'd1' }, rightPanelOpen: true, rightPanelTab: 'chat' } } });
    await new Promise(r => setTimeout(r, 10));

    expect(useAppStore.getState().currentProject?.project_id).toBe('proj-B');
    expect(useUIStore.getState().sidebarTab).toBe('docs');

    fetchSpy.mockRestore();
  });
});

// ── Persistence: loadGlobalPrefs ────────────────────────────────────────────

describe('loadGlobalPrefs', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('applies server data to store', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        theme: 'dark',
        language: 'ru',
        panelWidths: { left: 300, right: 400 },
      }),
    } as Response);

    await useUIStore.getState().loadGlobalPrefs();

    const s = useUIStore.getState();
    expect(s.theme).toBe('dark');
    expect(s.language).toBe('ru');
    expect(s.panelWidths).toEqual({ left: 300, right: 400 });
    expect(s.globalPrefsLoaded).toBe(true);
  });

  it('falls back to defaults on error', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network'));

    useUIStore.setState({ theme: 'light', globalPrefsLoaded: false });
    await useUIStore.getState().loadGlobalPrefs();

    expect(useUIStore.getState().theme).toBe('light');
    expect(useUIStore.getState().globalPrefsLoaded).toBe(true);
  });

  it('falls back to defaults on empty response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    } as Response);

    await useUIStore.getState().loadGlobalPrefs();

    expect(useUIStore.getState().globalPrefsLoaded).toBe(true);
    expect(useUIStore.getState().theme).toBe('light');
  });
});

// ── previewDocument cleared on document navigation ──────────────────────────

describe('previewDocument cleared on document switch', () => {
  it('clears previewDocument when navigating to a different document', () => {
    const docA = makeDoc({ document_id: 'doc-A' });
    const docB = makeDoc({ document_id: 'doc-B' });
    useAppStore.setState({
      previewDocument: docA,
      previewScrollOffset: 42,
      currentDocument: null,
    });
    useAppStore.getState().setCurrentDocument(docB);
    expect(useAppStore.getState().previewDocument).toBeNull();
    expect(useAppStore.getState().previewScrollOffset).toBeNull();
  });

  it('preserves previewDocument when setCurrentDocument is called with same doc (metadata refresh)', () => {
    const doc = makeDoc({ document_id: 'doc-A', title: 'Old' });
    useAppStore.setState({
      currentDocument: doc,
      previewDocument: doc,
      previewScrollOffset: 10,
    });
    useAppStore.getState().setCurrentDocument({ ...doc, title: 'Updated' });
    expect(useAppStore.getState().previewDocument).not.toBeNull();
    expect(useAppStore.getState().previewDocument?.title).toBe('Old');
    expect(useAppStore.getState().previewScrollOffset).toBe(10);
  });
});

// ── snapshotPreview cleared on project switch ────────────────────────────────

describe('snapshotPreview cleared on project switch', () => {
  it('clears snapshotPreview when switching to a different project', () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-A' }),
      snapshotPreview: { checkpoint_id: 'cp1', document_id: 'doc-1', content: '', label: '', comment: '', created_at: '' },
    });
    useAppStore.getState().setCurrentProject(makeProject({ project_id: 'proj-B' }));
    expect(useAppStore.getState().snapshotPreview).toBeNull();
  });

  it('clears snapshotPreview when setting project to null (logout)', () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-A' }),
      snapshotPreview: { checkpoint_id: 'cp1', document_id: 'doc-1', content: '', label: '', comment: '', created_at: '' },
    });
    useAppStore.getState().setCurrentProject(null);
    expect(useAppStore.getState().snapshotPreview).toBeNull();
  });
});

// ── liveHeadings (live TOC override) ──────────────────────────────────────────
// Guards the ephemeral client-derived headings slice and its reset-on-switch
// invariant: a stale entity's headings must never flash before the CM6 extension
// repopulates. See editor/live-headings-extension.ts.
describe('liveHeadings slice', () => {
  const items = [{ level: 1, text: 'H1', line: 1 }];

  it('setLiveHeadings stores items under the given entity id', () => {
    useAppStore.getState().setLiveHeadings('doc-1', items);
    expect(useAppStore.getState().liveHeadings).toEqual({ entityId: 'doc-1', items });
  });

  it('resets liveHeadings when switching reference (no stale flash)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }), liveHeadings: { entityId: 'ref-1', items } });
    useAppStore.getState().setCurrentReference(makeRef({ reference_id: 'ref-2' }));
    expect(useAppStore.getState().liveHeadings).toBeNull();
  });

  it('resets liveHeadings when clearing reference (back to document)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }), liveHeadings: { entityId: 'ref-1', items } });
    useAppStore.getState().setCurrentReference(null);
    expect(useAppStore.getState().liveHeadings).toBeNull();
  });

  it('resets liveHeadings when entering snapshot preview', () => {
    useAppStore.setState({ liveHeadings: { entityId: 'doc-1', items } });
    useAppStore.getState().setSnapshotPreview({ checkpoint_id: 'cp1', document_id: 'doc-1', content: '', label: '', comment: '', created_at: '' });
    expect(useAppStore.getState().liveHeadings).toBeNull();
  });
});

// currentTable is the table-focus pointer (analogous to currentReference for the
// References-panel "open table in center" flow). A table is NOT a separate entity — it is
// a CRDT subtree of the current document — so the pointer is doc-scoped and must clear on
// a document SWITCH (but survive a same-doc metadata refresh).
describe('setCurrentTable (table-focus pointer)', () => {
  const t = { document_id: 'doc-1', table_id: 't1' };

  it('sets and clears the currentTable pointer', () => {
    useAppStore.getState().setCurrentTable(t);
    expect(useAppStore.getState().currentTable).toEqual(t);
    useAppStore.getState().setCurrentTable(null);
    expect(useAppStore.getState().currentTable).toBeNull();
  });

  it('clears snapshotPreview when a table is focused (mutually exclusive center views)', () => {
    useAppStore.setState({ snapshotPreview: { checkpoint_id: 'cp1', document_id: 'doc-1', content: '', label: '', comment: '', created_at: '' } });
    useAppStore.getState().setCurrentTable(t);
    expect(useAppStore.getState().snapshotPreview).toBeNull();
  });

  it('clears currentTable on document SWITCH (table is doc-scoped)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'old' }) });
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'new' }));
    expect(useAppStore.getState().currentTable).toBeNull();
  });

  it('preserves currentTable on a same-document metadata refresh', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-1', title: 'refreshed' }));
    expect(useAppStore.getState().currentTable).toEqual(t);
  });
});

// currentTableLabel is the LIVE label for the focused table — resolved reactively from
// the document's `tables` list (see ReferencesPanel), so a rename by the local user OR a
// collaborative peer never leaves a stale label in the focus banner / header / split banner.
// It is reset to null everywhere currentTable resets (close, doc switch, project switch).
describe('currentTableLabel (live table label)', () => {
  const t = { document_id: 'doc-1', table_id: 't1' };

  it('setCurrentTableLabel sets and clears the live label', () => {
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentTableLabel('My table');
    expect(useAppStore.getState().currentTableLabel).toBe('My table');
    useAppStore.getState().setCurrentTableLabel(null);
    expect(useAppStore.getState().currentTableLabel).toBeNull();
  });

  it('setCurrentTable(null) resets currentTableLabel to null', () => {
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentTableLabel('My table');
    useAppStore.getState().setCurrentTable(null);
    expect(useAppStore.getState().currentTableLabel).toBeNull();
  });

  it('clears currentTableLabel on document SWITCH (table is doc-scoped)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'old' }) });
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentTableLabel('My table');
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'new' }));
    expect(useAppStore.getState().currentTableLabel).toBeNull();
  });

  it('opening a reference clears currentTableLabel (mutual exclusivity)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });
    useAppStore.getState().setCurrentTable(t);
    useAppStore.getState().setCurrentTableLabel('My table');
    useAppStore.getState().setCurrentReference(makeRef({ reference_id: 'r1' }));
    expect(useAppStore.getState().currentTableLabel).toBeNull();
  });
});

// focusDocument — the default openDocument path ("open the document BODY, not the
// remembered last-opened reference"; the tree passes restore and skips it). Same doc
// ⇒ unfocus the focused reference/table in place and return 'stayed' (no
// re-navigation); any other doc ⇒ clear the destination's remembered per-doc
// reference pointer and return 'navigate' so the caller's setCurrentDocument
// restoreFlow finds no pointer and opens the bare document.
describe('focusDocument (default openDocument path: open the document body)', () => {
  it('same doc with a focused reference: unfocuses it, clears the per-doc pointer, stays', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-1');
    useAppStore.getState().setCurrentReference(makeRef({ reference_id: 'ref-1' }));

    const out = useAppStore.getState().focusDocument('doc-1');

    expect(out).toBe('stayed');
    expect(useAppStore.getState().currentReference).toBeNull();
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-1')).toBeNull();
  });

  it('same doc with a focused table: clears the table focus, stays', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });
    useAppStore.getState().setCurrentTable({ document_id: 'doc-1', table_id: 't1' });

    const out = useAppStore.getState().focusDocument('doc-1');

    expect(out).toBe('stayed');
    expect(useAppStore.getState().currentTable).toBeNull();
  });

  it('same doc with nothing focused: navigates (the existing URL-sync path must still run)', () => {
    useAppStore.setState({ currentDocument: makeDoc({ document_id: 'doc-1' }) });

    const out = useAppStore.getState().focusDocument('doc-1');

    expect(out).toBe('navigate');
    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-1');
  });

  it('other doc with a persisted pointer: clears it and navigates; the following open restores no reference', async () => {
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-1' }),
    });
    useUIStore.getState().setCurrentReferenceForDoc('doc-2', 'ref-R5');

    const out = useAppStore.getState().focusDocument('doc-2');

    expect(out).toBe('navigate');
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-2')).toBeNull();

    // The refs list STILL contains R5 — restoreFlow must not resurrect it: the flag
    // path cleared the pointer, so the open commits ref-less.
    vi.spyOn(refsFetchNS, 'loadReferences').mockResolvedValue([
      makeRef({ reference_id: 'ref-R5', document_id: 'doc-2', content: '# R5' }),
    ]);
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-2' }));
    await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 0));

    expect(useAppStore.getState().currentDocument?.document_id).toBe('doc-2');
    expect(useAppStore.getState().currentReference).toBeNull();

    vi.restoreAllMocks();
  });

  it('pointer clear does not defeat a pendingReference: the pending ref wins the following commit (cross-doc ref-link shape)', () => {
    // openDocument's default path clears the target's remembered pointer BEFORE
    // setCurrentDocument; useEditorEvents' cross-doc ref link sets pendingReference
    // first. The commit must land the PENDING ref (applyRef), and re-write the
    // per-doc pointer for it — the clear must not resurrect the bare document.
    clearHydrateInFlight();
    useAppStore.setState({
      currentProject: makeProject({ project_id: 'proj-1' }),
      currentDocument: makeDoc({ document_id: 'doc-1' }),
    });
    // The doc-2 remembered pointer the default path is about to clear.
    useUIStore.getState().setCurrentReferenceForDoc('doc-2', 'ref-R5');
    const pending = makeRef({ reference_id: 'ref-R2', document_id: 'doc-2', content: '# R2' });
    useAppStore.getState().setPendingReference(pending);

    expect(useAppStore.getState().focusDocument('doc-2')).toBe('navigate');
    useAppStore.getState().setCurrentDocument(makeDoc({ document_id: 'doc-2' }));

    expect(useAppStore.getState().currentReference?.reference_id).toBe('ref-R2');
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-2')).toBe('ref-R2');
  });
});
