/**
 * ReferencesPanel — public-share scope parity (plan public-refs-panel-scope-parity).
 *
 * Mounts the REAL panel against real stores on the public surface and locks:
 *   1. rendered RefCard sequence = own batch (newest first) → parent batch →
 *      share-root batch, in the BACKEND payload order (order-preserving filter,
 *      never a client re-sort); sibling/descendant refs never render;
 *   2. the STORE list keeps the whole-subtree payload (public transclusion seeds
 *      from it — usePublicTransclusionSync);
 *   3. TableBadges derive statically from currentDocument content + tables_json
 *      (no collab handle exists on public);
 *   4. a doc whose scope has zero refs shows the EMPTY state, not foreign refs.
 *
 * Mocking follows PublicSharePage.test.tsx: vi.doMock + vi.resetModules, stable
 * `t` (a per-render new t loops the hydrate effect).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// NOTE: no static setPublicFileContext import — vi.resetModules() hands the
// panel a FRESH reference-url instance whose module-level context must be
// seeded inside beforeEach (a static import would write the stale instance).
import { tableAnchor } from './editor/live-preview/table-block-model';
import type { Document, Reference } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = '2026-01-01T00:00:00Z';

function mkDoc(id: string, parent_id: string | null, extra: Partial<Document> = {}): Document {
  return {
    document_id: id, project_id: '', parent_id, title: id, content: '', path: '',
    is_index: false, created_at: NOW, updated_at: NOW, ...extra,
  };
}

// Tree: root → A → A1 → A1child, root → B, root → Empty.
const TREE: Document[] = [
  mkDoc('root', null),
  mkDoc('A', 'root'),
  mkDoc('A1', 'A'),
  mkDoc('A1child', 'A1'),
  mkDoc('B', 'root'),
  mkDoc('Empty', 'root'),
];

const PARENT: Record<string, string | null> = {
  root: null, A: 'root', A1: 'A', A1child: 'A1', B: 'root', Empty: 'root',
};

const A1_CONTENT = `# A1

${tableAnchor('First', 't1')}

${tableAnchor('Second', 't2')}
`;
const A1_TABLES_JSON = JSON.stringify({
  t1: { columns: [100, 100], rows: [['a', 'b'], ['c', 'd']] },
  t2: { columns: [100], rows: [['x']] },
});

function mkRef(id: string, document_id: string, updated_at: string): Reference {
  return {
    reference_id: id, project_id: '', document_id, title: id, media_type: 'markdown',
    source_url: null, content: `body of ${id}`, processing_status: null, file_path: null,
    file_meta: null, created_at: NOW, updated_at,
  };
}

// Every subtree ref (the whole-subtree payload never depends on the open doc).
const ALL_REFS: Reference[] = [
  mkRef('a1-newest', 'A1', '2026-03-03T00:00:00Z'),
  mkRef('a1-older', 'A1', '2026-03-02T00:00:00Z'),
  mkRef('a-ref', 'A', '2026-02-20T00:00:00Z'),
  mkRef('root-ref', 'root', '2026-01-15T00:00:00Z'),
  mkRef('b-ref', 'B', '2026-04-01T00:00:00Z'),
  mkRef('a1child-ref', 'A1child', '2026-04-02T00:00:00Z'),
];

/** Mirror of the backend `sort_refs_by_depth_tier` cascade (sans archived sink):
 *  own tier → ancestors by proximity → flat fallback, newest `updated_at` first
 *  within each tier. The endpoint re-tiers per REQUESTED doc, so the mock must
 *  too — asserting the panel preserves the per-doc backend order. */
function payloadFor(docId: string): Reference[] {
  const chain: string[] = [];
  for (let cur: string | null = docId; cur !== null; cur = PARENT[cur] ?? null) chain.push(cur);
  const tier = new Map(chain.map((id, i) => [id, i]));
  return [...ALL_REFS].sort((a, b) => {
    const ta = tier.get(a.document_id ?? '') ?? chain.length;
    const tb = tier.get(b.document_id ?? '') ?? chain.length;
    if (ta !== tb) return ta - tb;
    return b.updated_at.localeCompare(a.updated_at);
  });
}

let container: HTMLDivElement;
let root: Root;
let ReferencesPanel: typeof import('./ReferencesPanel').ReferencesPanel;
let useAppStore: typeof import('../store/app-store').useAppStore;
let useUIStore: typeof import('../store/ui-store').useUIStore;
let loadReferencesMock: ReturnType<typeof vi.fn>;
let publicRefsMock: ReturnType<typeof vi.fn>;
let apiGetMock: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();

  const stableT = (k: string) => k;
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: stableT }) }));

  loadReferencesMock = vi.fn();
  publicRefsMock = vi.fn().mockImplementation((docId: string) => Promise.resolve(payloadFor(docId)));
  apiGetMock = vi.fn();
  vi.doMock('../api/references-fetch', () => ({ loadReferences: loadReferencesMock }));
  vi.doMock('../api/public-share', () => ({ publicReferencesByDoc: publicRefsMock }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: apiGetMock, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  }));

  ({ ReferencesPanel } = await import('./ReferencesPanel'));
  ({ useAppStore } = await import('../store/app-store'));
  ({ useUIStore } = await import('../store/ui-store'));
  // Seed the PUBLIC file context on the same fresh module instance the panel
  // reads (its publicToken gate): null here would drop the effect onto the
  // authed loadReferences branch (currentProject null → crash).
  const { setPublicFileContext } = await import('../utils/reference-url');
  setPublicFileContext('root');

  // Public surface flags: isPublicShare routes the panel to the anonymous fetch;
  // the file context carries the subtree root id (the panel's publicToken gate).
  useUIStore.setState({ isPublicShare: true });
  useAppStore.setState({
    currentProject: null,
    documents: TREE,
    currentDocument: mkDoc('A1', 'A', { content: A1_CONTENT, tables_json: A1_TABLES_JSON }),
    references: [],
    accessLevel: 'readonly',
  });

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  act(() => root.unmount());
  container.remove();
  const { setPublicFileContext } = await import('../utils/reference-url');
  setPublicFileContext(null);
  vi.doUnmock('../i18n');
  vi.doUnmock('../api/references-fetch');
  vi.doUnmock('../api/public-share');
  vi.doUnmock('../api/client');
});

async function flushEffects() {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

/** Ordered list-row bodies (ListPill roots carry role="button"; body = the
 *  first .text-sm span inside — the ref title / table label). */
function renderedRowTitles(): string[] {
  return Array.from(container.querySelectorAll('[role="button"]'))
    .map(el => el.querySelector('.text-sm')?.textContent ?? '');
}

describe('ReferencesPanel on public share', () => {
  it('renders own → parent → share-root batches in backend order; badges lead; foreign refs never render', async () => {
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    expect(renderedRowTitles()).toEqual([
      'First', 'Second',                       // table badges (anchor order)
      'a1-newest', 'a1-older',                 // own batch, newest first
      'a-ref',                                 // parent batch
      'root-ref',                              // share-root batch
    ]);
    expect(container.textContent).not.toContain('b-ref');
    expect(container.textContent).not.toContain('a1child-ref');
  });

  it('keeps the whole-subtree payload in the store (public transclusion parity)', async () => {
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    const storeRefs = useAppStore.getState().references;
    expect(storeRefs.map(r => r.reference_id)).toEqual(payloadFor('A1').map(r => r.reference_id));
  });

  it('uses the anonymous per-doc fetch only (no authed loadReferences, no agent-config GET)', async () => {
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    expect(publicRefsMock).toHaveBeenCalledWith('A1');
    expect(loadReferencesMock).not.toHaveBeenCalled();
    expect(apiGetMock).not.toHaveBeenCalled();
  });

  it('re-filters on doc switch: opening sibling B shows B\'s own batch then the root batch (re-tiered payload order preserved)', async () => {
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    act(() => {
      useAppStore.setState({ currentDocument: mkDoc('B', 'root', { content: '', tables_json: null }) });
    });
    await flushEffects();

    // B's backend tier order: own batch (b-ref) → share-root batch (root-ref);
    // sibling/descendant branches (A/A1/A1child) never render.
    expect(renderedRowTitles()).toEqual(['b-ref', 'root-ref']);
  });

  it('shows the empty state (not foreign refs) when the open doc\'s scope has zero refs', async () => {
    // A payload whose only refs belong to branches OUTSIDE Empty's {Empty, root}
    // scope (root owns none here) — the panel must show the empty state, and the
    // foreign refs must NOT render behind it.
    publicRefsMock.mockResolvedValue(
      ALL_REFS.filter(r => r.document_id === 'B' || r.document_id === 'A1child'),
    );
    act(() => {
      useAppStore.setState({ currentDocument: mkDoc('Empty', 'root', { content: '', tables_json: null }) });
    });
    act(() => root.render(createElement(ReferencesPanel)));
    await flushEffects();

    expect(renderedRowTitles()).toEqual([]);
    expect(container.textContent).toContain('dragAndDrop');
  });
});
