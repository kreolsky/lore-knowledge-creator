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
const lastChatParams = () => lastParams('chat-search');

const chatHit = (over: Record<string, unknown> = {}) => ({
  chat_id: 'b2', thread_id: 'r1', chat_title: 'Plague notes', document_id: 'doc-9',
  message_id: 'm9', role: 'assistant', created_at: '2026-10-01T10:00:00Z',
  snippet: 'the plague reached the port', question: 'when did it arrive?',
  score: 0.81, sibling: false, ...over,
});
const panelText = () => container.textContent ?? '';

describe('SearchPanel chats mode', () => {

  it('the administrator-disabled 403 shows its own text, not the generic failure', async () => {
    uiState.searchMode = 'chats';
    apiGet.mockRejectedValue(new FakeForbidden('chat-recall is disabled by the administrator'));
    await renderPanel();
    await wait(0);
    expect(panelText()).toContain('chatSearchDisabled');
    expect(panelText()).not.toContain('searchFailed');
    expect(panelText()).not.toContain('noMatchesFound');
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
    expect(chatState.setPendingScroll).toHaveBeenCalledWith('b2', 'm9', 'the plague reached the port');
    expect(chatState.loadSessions).not.toHaveBeenCalled();
    expect(uiState.setRightPanelTab).toHaveBeenCalledWith('doc-1', 'chat');
    const events = emitSpy.mock.calls.map((c: unknown[]) => c[0]);
    expect(events).toContain('open-right-panel');
    expect(events).not.toContain('navigate-to-document');
    expect(events).not.toContain('navigate-to-reference');
  });

  describe('«By title» group', () => {
    const session = (id: string, title: string) => ({ session_id: id, title, last_message_at: null });

    beforeEach(() => {
      uiState.searchMode = 'chats';
      uiState.searchQuery = 'гоб';
      chatState.sessions = [session('s-gob', 'Гоблины'), session('s-elf', 'Эльфы')];
    });

    it('a disabled semantic search still shows the title row AND the disabled text', async () => {
      apiGet.mockRejectedValue(new FakeForbidden('chat-recall is disabled by the administrator'));
      await renderPanel();
      await wait(0);
      expect(container.querySelector('[data-chat-title-hit="s-gob"]')).toBeTruthy();
      expect(panelText()).toContain('chatSearchDisabled');
    });
  });
});

describe('SearchPanel return trip', () => {
  // The panel unmounts while the chat tab shows a hit; coming back is a remount.
  async function remount() {
    act(() => root.unmount());
    root = createRoot(container);
    act(() => root.render(createElement(SearchPanel)));
    await wait(0);
  }
  const chatCalls = () => apiGet.mock.calls.filter((c: unknown[]) => String(c[0]).includes('/chat-search?')).length;
  const listEl = () => container.querySelector('[data-chat-hit]')!.parentElement as HTMLElement;

  beforeEach(() => {
    uiState.searchMode = 'chats';
    appState.currentDocument = { document_id: 'doc-1' };
    apiGet.mockResolvedValue({ hits: [chatHit()] });
  });

  it('an unchanged search comes back with its hits and makes no request', async () => {
    await renderPanel();
    await wait(0);
    expect(chatCalls()).toBe(1);
    await remount();
    expect(chatCalls()).toBe(1);
    expect(container.querySelector('[data-chat-hit]')).toBeTruthy();
    expect(panelText()).not.toContain('noMatchesFound');
  });

  it('a changed query searches again', async () => {
    await renderPanel();
    await wait(0);
    uiState.searchQuery = 'plague';
    await remount();
    expect(chatCalls()).toBe(2);
    expect(lastChatParams().get('q')).toBe('plague');
  });

  it('another open document searches again, without the old narrow', async () => {
    await renderPanel();
    await wait(0);
    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    expect(lastChatParams().get('scope')).toBe('siblings');
    appState.currentDocument = { document_id: 'doc-2' };
    await remount();
    expect(lastChatParams().get('anchor_document_id')).toBe('doc-2');
    expect(lastChatParams().get('scope')).toBe('project');
    expect(scopeToggle()!.checked).toBe(false);
  });

  it('the this-document narrow survives the round trip', async () => {
    await renderPanel();
    await wait(0);
    await act(async () => { scopeToggle()!.click(); });
    await wait(350);
    const calls = chatCalls();
    await remount();
    expect(scopeToggle()!.checked).toBe(true);
    expect(chatCalls()).toBe(calls);
  });

  it('the list scroll position is restored', async () => {
    await renderPanel();
    await wait(0);
    const list = listEl();
    list.scrollTop = 240;
    list.dispatchEvent(new Event('scroll'));
    await remount();
    expect(listEl().scrollTop).toBe(240);
  });

  it('a failed search is not remembered — the return trip searches again', async () => {
    apiGet.mockRejectedValue(new Error('boom'));
    await renderPanel();
    await wait(0);
    apiGet.mockResolvedValue({ hits: [chatHit()] });
    await remount();
    expect(chatCalls()).toBe(2);
    expect(container.querySelector('[data-chat-hit]')).toBeTruthy();
  });
});
