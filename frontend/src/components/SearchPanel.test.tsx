/**
 * SearchPanel — scope row wiring, both modes.
 *
 * The retrieval machinery is backend-tested; here we assert only the WIRING the
 * component adds:
 *  - BOTH modes render a scope row, and each carries its own second control:
 *    «Memory only» in semantic, «Exact match» in fulltext;
 *  - the «In this document» toggle is shared by both modes, is anchored to the
 *    current document and is HIDDEN (not disabled) when none is open;
 *  - the «Memory only» checkbox maps onto include_docs=false&include_refs=false
 *    (no third param);
 *  - fulltext sends the same under_document_id the semantic mode sends — the
 *    label means one scope (root + descendants) across both modes, enforced
 *    backend-side by the shared subtree_doc_ids (see projects.py WHY
 *    under_document_id);
 *  - toggling any control re-runs the search (asserted on apiClient args).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let SearchPanel: typeof import('./SearchPanel').SearchPanel;
let apiGet: ReturnType<typeof vi.fn>;
let appState: Record<string, unknown>;
let uiState: Record<string, unknown>;

const wait = (ms: number) => act(async () => { await new Promise(r => setTimeout(r, ms)); });

beforeEach(async () => {
  vi.resetModules();

  apiGet = vi.fn().mockResolvedValue({ hits: [] });
  vi.doMock('../api/client', () => ({ apiClient: { get: apiGet } }));
  vi.doMock('../events', () => ({ emit: vi.fn() }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  vi.doMock('./ui', () => ({
    Button: (p: any) => createElement('button', { type: 'button', onClick: p.onClick }, p.children),
    FieldInput: (p: any) => createElement('input', {
      value: p.value, placeholder: p.placeholder, onChange: p.onChange,
    }),
    FieldCheckbox: (p: any) => createElement('label', null,
      createElement('input', {
        type: 'checkbox',
        'data-label': p.label,
        checked: p.checked,
        onChange: (e: any) => p.onChange(e.target.checked),
      }),
      p.label,
    ),
    ListPill: (p: any) => createElement('div', { onClick: p.onClick }, p.children),
    PillList: (p: any) => createElement('div', { ref: p.ref }, p.children),
  }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({ Search: icon, Sparkles: icon }));

  appState = {
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  uiState = {
    searchQuery: 'vertigo',
    searchResults: [],
    searchMode: 'semantic',
    setSearchCache: vi.fn(),
    setSearchMode: vi.fn(),
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: any) => any) => selector(appState),
      { getState: () => appState },
    ),
  }));
  vi.doMock('../store/ui-store', () => ({
    useUIStore: Object.assign(
      (selector: (s: any) => any) => selector(uiState),
      { getState: () => uiState, setState: (patch: Record<string, unknown>) => { Object.assign(uiState, patch); } },
    ),
  }));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../api/client');
  vi.doUnmock('../events');
  vi.doUnmock('../i18n');
  vi.doUnmock('./ui');
  vi.doUnmock('lucide-react');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../store/ui-store');
});

function renderPanel() {
  return import('./SearchPanel').then((m) => {
    SearchPanel = m.SearchPanel;
    act(() => root.render(createElement(SearchPanel)));
  });
}

const scopeToggle = () => container.querySelector('input[data-label="searchScopeThisDoc"]') as HTMLInputElement | null;
const memoryBox = () => container.querySelector('input[data-label="searchMemoryOnly"]') as HTMLInputElement | null;
const exactBox = () => container.querySelector('input[data-label="searchExactMatch"]') as HTMLInputElement | null;

/** Last call to `endpoint`, parsed into params. */
function lastParams(endpoint: 'semantic-search' | 'search'): URLSearchParams {
  // `search` is a substring of `semantic-search`, so the fulltext filter must
  // exclude the semantic route rather than match on the bare word.
  const calls = apiGet.mock.calls.filter((c: unknown[]) => {
    const path = String(c[0]);
    return endpoint === 'semantic-search'
      ? path.includes('semantic-search')
      : path.includes('/search?') && !path.includes('semantic-search');
  });
  expect(calls.length, `no ${endpoint} call was made`).toBeGreaterThan(0);
  const url = new URL(String(calls[calls.length - 1][0]), 'http://x');
  return url.searchParams;
}

const lastSemanticParams = () => lastParams('semantic-search');
const lastFulltextParams = () => lastParams('search');

describe('SearchPanel semantic scope row', () => {
  it('semantic mode with a current document renders both scope controls', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    expect(scopeToggle()).toBeTruthy();
    expect(memoryBox()).toBeTruthy();
  });

  it('fulltext mode renders the this-document narrow and the exact toggle, never memory-only', async () => {
    uiState.searchMode = 'fulltext';
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    expect(scopeToggle()).toBeTruthy();
    expect(exactBox()).toBeTruthy();
    // Memory is a semantic-only corpus — the fulltext scan has no memory layer,
    // so the checkbox must not appear where it would do nothing.
    expect(memoryBox()).toBeNull();
  });

  it('semantic mode never renders the exact toggle', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    expect(exactBox()).toBeNull();
  });

  it('fulltext with no current document hides the toggle, keeps exact', async () => {
    uiState.searchMode = 'fulltext';
    await renderPanel();
    expect(scopeToggle()).toBeNull();
    expect(exactBox()).toBeTruthy();
  });

  it('no current document hides the this-document toggle, keeps memory-only', async () => {
    await renderPanel();
    expect(scopeToggle()).toBeNull();
    expect(memoryBox()).toBeTruthy();
  });

  it('default semantic request carries only q (whole project, all kinds)', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);
    const params = lastSemanticParams();
    expect(params.get('q')).toBe('vertigo');
    expect(params.has('under_document_id')).toBe(false);
    expect(params.has('include_docs')).toBe(false);
    expect(params.has('include_refs')).toBe(false);
  });

  it('memory-only checkbox flips BOTH include params in the request', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);

    await act(async () => { memoryBox()!.click(); });
    await wait(350); // debounced re-run
    let params = lastSemanticParams();
    expect(params.get('include_docs')).toBe('false');
    expect(params.get('include_refs')).toBe('false');
    expect(params.has('include_memory')).toBe(false); // server default: true

    await act(async () => { memoryBox()!.click(); });
    await wait(350);
    params = lastSemanticParams();
    expect(params.has('include_docs')).toBe(false);
    expect(params.has('include_refs')).toBe(false);
  });

  it('this-document toggle adds under_document_id=currentDocument', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);

    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    const params = lastSemanticParams();
    expect(params.get('under_document_id')).toBe('doc-1');

    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    expect(lastSemanticParams().has('under_document_id')).toBe(false);
  });

  it('both controls combine: memory-only + subtree', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);

    await act(async () => { scopeToggle()!.click(); });
    await act(async () => { memoryBox()!.click(); });
    await wait(350);
    const params = lastSemanticParams();
    expect(params.get('under_document_id')).toBe('doc-1');
    expect(params.get('include_docs')).toBe('false');
    expect(params.get('include_refs')).toBe('false');
  });

  it('default fulltext request carries only q (whole project, no exact)', async () => {
    uiState.searchMode = 'fulltext';
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);
    const params = lastFulltextParams();
    expect(params.get('q')).toBe('vertigo');
    expect(params.has('under_document_id')).toBe(false);
    expect(params.has('exact')).toBe(false);
  });

  it('fulltext this-document toggle sends the SAME under_document_id semantic sends', async () => {
    uiState.searchMode = 'fulltext';
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);

    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    expect(lastFulltextParams().get('under_document_id')).toBe('doc-1');

    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    expect(lastFulltextParams().has('under_document_id')).toBe(false);
  });

  it('fulltext exact toggle adds exact=true and drops it again', async () => {
    uiState.searchMode = 'fulltext';
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);

    await act(async () => { exactBox()!.click(); });
    await wait(350);
    expect(lastFulltextParams().get('exact')).toBe('true');

    await act(async () => { exactBox()!.click(); });
    await wait(350);
    expect(lastFulltextParams().has('exact')).toBe(false);
  });
});
