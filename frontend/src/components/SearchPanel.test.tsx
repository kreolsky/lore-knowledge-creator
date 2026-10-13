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
 *  - toggling any control re-runs the search (asserted on apiClient args);
 *  - «Chats» mode: the request's scope/anchor, its error/empty/disabled states,
 *    and a hit click that opens the chat at the message WITHOUT navigating;
 *    the client-side «By title» group above the semantic hits, which survives a
 *    disabled/failed semantic request.
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
let emitSpy: ReturnType<typeof vi.fn>;
let chatState: Record<string, any>;
let scopeLoaded: boolean;
let appState: Record<string, unknown>;
let uiState: Record<string, unknown>;

// jsdom lacks Element.scrollTo (switchMode resets the list's scroll) — same
// polyfill as MessageList.test.tsx.
if (!Element.prototype.scrollTo) {
  (Element.prototype as unknown as { scrollTo: () => void }).scrollTo = () => {};
}

class FakeForbidden extends Error {
  detail: unknown;
  constructor(detail?: unknown) { super('Access forbidden'); this.detail = detail; }
}

const wait = (ms: number) => act(async () => { await new Promise(r => setTimeout(r, ms)); });

beforeEach(async () => {
  vi.resetModules();

  apiGet = vi.fn().mockResolvedValue({ hits: [] });
  vi.doMock('../api/client', () => ({ apiClient: { get: apiGet }, ForbiddenError: FakeForbidden }));
  emitSpy = vi.fn();
  vi.doMock('../events', () => ({ emit: emitSpy }));
  scopeLoaded = true;
  vi.doMock('../chat/scope-tracker', () => ({
    isChatScopeLoaded: () => scopeLoaded,
    markChatScopeLoaded: () => { scopeLoaded = true; },
  }));
  chatState = {
    sessions: [],
    setActiveSession: vi.fn((id: string | null) => { chatState.activeSessionId = id; }),
    openBranch: vi.fn(),
    setPendingScroll: vi.fn(),
    loadSessions: vi.fn(async () => {}),
  };
  vi.doMock('../store/chat-store', () => ({
    useChatStore: Object.assign(
      (selector: (s: any) => any) => selector(chatState),
      { getState: () => chatState },
    ),
  }));
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
    // data-* pass through verbatim, as the real ListPill renders them on its root.
    ListPill: ({ onClick, children, variant: _v, ...rest }: any) => createElement('div', { onClick, ...rest }, children),
    PillList: (p: any) => createElement('div', { ref: p.ref }, p.children),
  }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({ Search: icon, Sparkles: icon, MessagesSquare: icon }));

  appState = {
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  uiState = {
    searchQuery: 'vertigo',
    searchResults: [],
    searchMode: 'semantic',
    setSearchCache: vi.fn(),
    setSearchMode: vi.fn((m: string) => { uiState.searchMode = m; }),
    setRightPanelTab: vi.fn(),
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
  vi.doUnmock('../chat/scope-tracker');
  vi.doUnmock('../store/chat-store');
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
function lastParams(endpoint: 'semantic-search' | 'search' | 'chat-search'): URLSearchParams {
  // `search` is a substring of `semantic-search` and `chat-search`, so the
  // fulltext filter must match the bare route segment rather than the word.
  const calls = apiGet.mock.calls.filter((c: unknown[]) => {
    const path = String(c[0]);
    return endpoint === 'search' ? path.includes('/search?') : path.includes(`/${endpoint}?`);
  });
  expect(calls.length, `no ${endpoint} call was made`).toBeGreaterThan(0);
  const url = new URL(String(calls[calls.length - 1][0]), 'http://x');
  return url.searchParams;
}

const lastSemanticParams = () => lastParams('semantic-search');
const lastFulltextParams = () => lastParams('search');
const lastChatParams = () => lastParams('chat-search');

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

const chatHit = (over: Record<string, unknown> = {}) => ({
  chat_id: 'b2', thread_id: 'r1', chat_title: 'Plague notes', document_id: 'doc-9',
  message_id: 'm9', role: 'assistant', created_at: '2026-10-01T10:00:00Z',
  snippet: 'the plague reached the port', question: 'when did it arrive?',
  score: 0.81, sibling: false, ...over,
});
const panelText = () => container.textContent ?? '';
const modeButton = (key: string) =>
  Array.from(container.querySelectorAll('button')).find(b => b.textContent === key) as HTMLButtonElement;

describe('SearchPanel chats mode', () => {
  it('switching to «Chats» issues /chat-search in project scope anchored on the open document', async () => {
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);
    await act(async () => { modeButton('searchModeChats').click(); });
    await wait(250);
    const params = lastChatParams();
    expect(params.get('q')).toBe('vertigo');
    expect(params.get('scope')).toBe('project');
    expect(params.get('anchor_document_id')).toBe('doc-1');
    expect(params.has('under_document_id')).toBe(false);
  });

  it('«In this document» maps to scope=siblings with the open document as the anchor', async () => {
    uiState.searchMode = 'chats';
    appState.currentDocument = { document_id: 'doc-1' };
    await renderPanel();
    await wait(0);
    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    const params = lastChatParams();
    expect(params.get('scope')).toBe('siblings');
    expect(params.get('anchor_document_id')).toBe('doc-1');
    // Memory-only is a documents-corpus switch — it never rides a chat search.
    expect(memoryBox()).toBeNull();
  });

  it('no open document: project scope, no anchor, no this-document toggle', async () => {
    uiState.searchMode = 'chats';
    await renderPanel();
    await wait(0);
    const params = lastChatParams();
    expect(params.get('scope')).toBe('project');
    expect(params.has('anchor_document_id')).toBe(false);
    expect(scopeToggle()).toBeNull();
  });

  it('a failed request shows the error row, not the empty state', async () => {
    uiState.searchMode = 'chats';
    apiGet.mockRejectedValue(new Error('boom'));
    await renderPanel();
    await wait(0);
    lastChatParams();
    expect(panelText()).toContain('searchFailed');
    expect(panelText()).not.toContain('noMatchesFound');
  });

  it('the administrator-disabled 403 shows its own text, not the generic failure', async () => {
    uiState.searchMode = 'chats';
    apiGet.mockRejectedValue(new FakeForbidden('chat-recall is disabled by the administrator'));
    await renderPanel();
    await wait(0);
    expect(panelText()).toContain('chatSearchDisabled');
    expect(panelText()).not.toContain('searchFailed');
    expect(panelText()).not.toContain('noMatchesFound');
  });

  it('no hits shows the empty state', async () => {
    uiState.searchMode = 'chats';
    await renderPanel();
    await wait(0);
    lastChatParams();
    expect(panelText()).toContain('noMatchesFound');
    expect(panelText()).not.toContain('searchFailed');
  });

  it('renders a hit with its chat title and snippet, and marks a sibling hit', async () => {
    uiState.searchMode = 'chats';
    apiGet.mockResolvedValue({ hits: [chatHit({ sibling: true }), chatHit({ chat_id: 'c3', message_id: 'm3', chat_title: 'Elsewhere' })] });
    await renderPanel();
    await wait(0);
    expect(panelText()).toContain('Plague notes');
    expect(panelText()).toContain('the plague reached the port');
    expect(container.querySelectorAll('[data-sibling="true"]').length).toBe(1);
  });

  it('a hit click opens the hit\'s branch on its thread root, scrolls to the message and never navigates', async () => {
    uiState.searchMode = 'chats';
    appState.currentDocument = { document_id: 'doc-1' };
    apiGet.mockResolvedValue({ hits: [chatHit()] });
    await renderPanel();
    await wait(0);
    const row = container.querySelector('[data-chat-hit]') as HTMLElement;
    await act(async () => { row.click(); });
    await wait(0);
    expect(chatState.openBranch).toHaveBeenCalledWith('b2', 'r1');
    expect(chatState.setPendingScroll).toHaveBeenCalledWith('b2', 'm9');
    expect(chatState.loadSessions).not.toHaveBeenCalled();
    expect(uiState.setRightPanelTab).toHaveBeenCalledWith('doc-1', 'chat');
    const events = emitSpy.mock.calls.map((c: unknown[]) => c[0]);
    expect(events).toContain('open-right-panel');
    expect(events).not.toContain('navigate-to-document');
    expect(events).not.toContain('navigate-to-reference');
  });

  it('a hit click before the chat list was ever loaded loads it first, then opens', async () => {
    uiState.searchMode = 'chats';
    appState.currentDocument = { document_id: 'doc-1' };
    apiGet.mockResolvedValue({ hits: [chatHit()] });
    scopeLoaded = false;
    const order: string[] = [];
    chatState.loadSessions = vi.fn(async () => { order.push('load'); });
    chatState.openBranch = vi.fn(() => { order.push('open'); });
    await renderPanel();
    await wait(0);
    await act(async () => { (container.querySelector('[data-chat-hit]') as HTMLElement).click(); });
    await wait(0);
    expect(chatState.loadSessions).toHaveBeenCalledWith('p1', 'doc-1');
    expect(order).toEqual(['load', 'open']);
  });

  describe('«By title» group', () => {
    const session = (id: string, title: string) => ({ session_id: id, title, last_message_at: null });

    beforeEach(() => {
      uiState.searchMode = 'chats';
      uiState.searchQuery = 'гоб';
      chatState.sessions = [session('s-gob', 'Гоблины'), session('s-elf', 'Эльфы')];
    });

    it('renders the matching chat as a title row before any semantic hit', async () => {
      apiGet.mockResolvedValue({ hits: [chatHit()] });
      await renderPanel();
      await wait(0);
      const titleRows = Array.from(container.querySelectorAll('[data-chat-title-hit]'));
      expect(titleRows.map(r => r.getAttribute('data-chat-title-hit'))).toEqual(['s-gob']);
      expect(titleRows[0].textContent).toContain('chatSearchTitleBadge');
      const semanticRow = container.querySelector('[data-chat-hit]')!;
      expect(titleRows[0].compareDocumentPosition(semanticRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      expect(panelText()).not.toContain('noMatchesFound');
    });

    it('a disabled semantic search still shows the title row AND the disabled text', async () => {
      apiGet.mockRejectedValue(new FakeForbidden('chat-recall is disabled by the administrator'));
      await renderPanel();
      await wait(0);
      expect(container.querySelector('[data-chat-title-hit="s-gob"]')).toBeTruthy();
      expect(panelText()).toContain('chatSearchDisabled');
    });

    it('a title row click opens that chat in the Chat tab without a message scroll', async () => {
      appState.currentDocument = { document_id: 'doc-1' };
      await renderPanel();
      await wait(0);
      await act(async () => { (container.querySelector('[data-chat-title-hit="s-gob"]') as HTMLElement).click(); });
      await wait(0);
      expect(chatState.activeSessionId).toBe('s-gob');
      expect(chatState.setPendingScroll).not.toHaveBeenCalled();
      expect(uiState.setRightPanelTab).toHaveBeenCalledWith('doc-1', 'chat');
      expect(emitSpy.mock.calls.map((c: unknown[]) => c[0])).toContain('open-right-panel');
    });

    it('an unloaded chat scope is loaded once, and its list feeds the title rows', async () => {
      scopeLoaded = false;
      const loaded = chatState.sessions;
      chatState.sessions = [];
      chatState.loadSessions = vi.fn(async () => { chatState.sessions = loaded; });
      await renderPanel();
      await wait(0);
      expect(chatState.loadSessions).toHaveBeenCalledTimes(1);
      expect(container.querySelector('[data-chat-title-hit="s-gob"]')).toBeTruthy();
    });

    it('below the chats minimum no title row renders', async () => {
      uiState.searchQuery = 'г';
      await renderPanel();
      await wait(0);
      expect(container.querySelector('[data-chat-title-hit]')).toBeNull();
    });
  });
});
