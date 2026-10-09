/**
 * Unit tests for ChatHeader — ghost plaque, new-chat no-op, goto-parent
 * visibility rules (icon lives in the reserved LEFT slot), the always-reserved
 * title slot, double-click rename, and the optimistic rename commit. Mounted
 * with mocked stores per the repo's component-test pattern.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const chatState: Record<string, unknown> = {
  sessions: [],
  activeSessionId: null,
  modelsLoaded: true,
  listFilter: null,
};
const chatActions = {
  setListFilter: vi.fn((v: string | null) => { chatState.listFilter = v; }),
  deleteSession: vi.fn(),
  updateSession: vi.fn().mockResolvedValue(undefined),
  loadModels: vi.fn(),
  startGhostChat: vi.fn(),
};
vi.mock('../../store/chat-store', () => ({
  useChatStore: Object.assign((sel: (s: unknown) => unknown) => sel({ ...chatState, ...chatActions }), {
    getState: () => ({ ...chatState, ...chatActions, sessions: chatState.sessions }),
    setState: (fn: (s: unknown) => unknown) => { Object.assign(chatState, fn(chatState)); },
  }),
  selectBranchPath: () => [],
}));
const appState: Record<string, unknown> = {
  currentProject: { project_id: 'p1' },
  currentDocument: { document_id: 'doc-1' },
  currentReference: null,
  documents: [],
};
vi.mock('../../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));
const uiState: Record<string, unknown> = {
  getChatSortByProximity: () => false,
  setChatSortByProximity: vi.fn(),
  pinRightPanel: vi.fn(),
  documents: {},
};
vi.mock('../../store/ui-store', () => ({
  useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiState), { getState: () => uiState }),
}));
const emitted: string[] = [];
vi.mock('../../events', () => ({ emit: (...a: unknown[]) => emitted.push(String(a[0])) }));
vi.mock('../../chat/navigate', () => ({ navigateToDocKeepingChat: vi.fn() }));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../hooks/useArmedAction', () => ({
  useArmedAction: () => ({ armed: false, handleClick: (fn: () => void) => fn(), disarm: () => {} }),
}));

import { ChatHeader } from './ChatHeader';

let root: Root | null = null;
function mount() {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(ChatHeader)));
  return host;
}
function unmount() {
  // WHY the local const: `root` is a mutable module-level `let`, so TS drops the
  // null-narrowing inside the arrow closure passed to act().
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

beforeEach(() => {
  vi.clearAllMocks();
  emitted.length = 0;
  chatState.sessions = [];
  chatState.activeSessionId = null;
  chatState.listFilter = null;
  appState.currentDocument = { document_id: 'doc-1' };
  appState.currentReference = null;
});

describe('ChatHeader', () => {
  it('ghost state renders the new-chat plaque, not the session toolbar', () => {
    const host = mount();
    expect(host.textContent).toContain('chatEmptyHint');
    expect(host.textContent).not.toContain('chatNew');
    unmount();
  });

  it('ghost plaque: search icon enters title-search mode; Escape leaves it', () => {
    const host = mount();
    expect(host.querySelector('[data-chat-search-input]')).toBeNull();
    const btn = host.querySelector<HTMLButtonElement>('[title="chatSearchTitles"]');
    expect(btn, 'search icon in the ghost plaque').toBeTruthy();
    act(() => btn!.click());
    expect(chatActions.setListFilter).toHaveBeenCalledWith('');
    unmount();
    // Re-mount in search mode (the mocked store is not reactive; the unmount
    // cleanup above legitimately reset it to null, so seed the mode by hand).
    chatState.listFilter = '';
    const host2 = mount();
    const input = host2.querySelector<HTMLInputElement>('[data-chat-search-input]');
    expect(input, 'input replaces the hint').toBeTruthy();
    expect(host2.textContent).not.toContain('chatEmptyHint');
    act(() => { input!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
    expect(chatActions.setListFilter).toHaveBeenLastCalledWith(null);
    unmount();
  });

  it('ghost plaque: blur with an empty query leaves search mode; a typed query stays', () => {
    chatState.listFilter = '';
    let host = mount();
    let input = host.querySelector<HTMLInputElement>('[data-chat-search-input]')!;
    act(() => { input.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(chatActions.setListFilter).toHaveBeenLastCalledWith(null);
    unmount();
    chatActions.setListFilter.mockClear();
    chatState.listFilter = 'abc';
    host = mount();
    input = host.querySelector<HTMLInputElement>('[data-chat-search-input]')!;
    act(() => { input.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(chatActions.setListFilter).not.toHaveBeenCalled();
    unmount();
  });

  it('search is not offered once a chat is open, and opening one clears the filter', () => {
    chatState.listFilter = 'abc';
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-1', reference_id: null }];
    const host = mount();
    expect(host.querySelector('[title="chatSearchTitles"]')).toBeNull();
    expect(host.querySelector('[data-chat-search-input]')).toBeNull();
    expect(chatActions.setListFilter).toHaveBeenCalledWith(null);
    unmount();
  });

  it('unmount (tab switch) clears the filter', () => {
    chatState.listFilter = 'abc';
    mount();
    chatActions.setListFilter.mockClear();
    unmount();
    expect(chatActions.setListFilter).toHaveBeenCalledWith(null);
  });

  it('doc session whose parent is the OPEN doc shows no goto-parent button', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-1', reference_id: null }];
    const host = mount();
    expect(host.textContent).not.toContain('gotoParentDoc');
    unmount();
  });

  it('doc session whose parent is the OPEN doc shows the change-parent icon in the slot', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-1', reference_id: null }];
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot, 'reserved slot rendered').toBeTruthy();
    // Exactly the plan's empty-slot case: parent == open doc → change-parent
    // icon (FolderUp) renders, goto-parent does not.
    expect(slot!.querySelector('[title="changeChatParent"]'), 'change-parent inside slot').toBeTruthy();
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'goto-parent stays hidden').toBeNull();
    unmount();
  });

  it('doc session under a DIFFERENT doc shows goto-parent, not change-parent', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-2', reference_id: null }];
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'goto-parent inside slot').toBeTruthy();
    expect(slot!.querySelector('[title="changeChatParent"]'), 'no change-parent').toBeNull();
    unmount();
  });

  it('ref session NEVER gets the change-parent icon (even with its ref open, slot empty)', () => {
    chatState.activeSessionId = 's2';
    chatState.sessions = [{ session_id: 's2', document_id: 'doc-1', reference_id: 'ref-9', reference_title: 'R' }];
    appState.currentReference = { reference_id: 'ref-9' };
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot, 'reserved slot rendered').toBeTruthy();
    // A ref-scoped chat's parent is the reference; with it open neither icon
    // renders — the guard is on isRefSession explicitly, not !showGotoParent.
    expect(slot!.querySelector('[title="changeChatParent"]'), 'no change-parent for ref session').toBeNull();
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'no goto-parent (ref is open)').toBeNull();
    unmount();
  });

  it('no pencil icon is rendered (rename is double-click only)', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-2', reference_id: null }];
    const host = mount();
    expect(host.querySelector('[title="renameChat"]')).toBeNull();
    unmount();
  });

  it('title slot is always reserved and never holds a goto-parent icon when hidden', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-1', reference_id: null }];
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot, 'reserved slot rendered').toBeTruthy();
    // The slot keeps its 22px footprint so the title edge never shifts; with
    // the parent == open doc it now carries the change-parent icon, never a
    // goto-parent one (plan chat-reparent-from-header).
    expect(slot!.querySelector('[title^="gotoParentDoc"]')).toBeNull();
    unmount();
  });

  it('goto-parent icon renders INSIDE the reserved left slot', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-2', reference_id: null }];
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot, 'reserved slot rendered').toBeTruthy();
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'goto-parent inside slot').toBeTruthy();
    unmount();
  });

  it('ref session navigates to its REFERENCE (never the owning doc)', () => {
    chatState.activeSessionId = 's2';
    chatState.sessions = [{ session_id: 's2', document_id: 'doc-1', reference_id: 'ref-9', reference_title: 'R' }];
    const host = mount();
    // The affordance is identified by its title ATTRIBUTE (not text content).
    const btn = host.querySelector('[title^="gotoParentDoc"]');
    expect(btn, 'goto-parent affordance rendered').toBeTruthy();
    act(() => btn!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(emitted).toContain('navigate-to-reference');
    unmount();
  });

  it('rename commits optimistically and calls updateSession once', async () => {
    chatState.activeSessionId = 's3';
    chatState.sessions = [{ session_id: 's3', document_id: 'doc-2', reference_id: null, title: 'old' }];
    const host = mount();
    // Rename entry point is a DOUBLE-CLICK on the title (the pencil icon is gone).
    const title = host.querySelector('[data-chat-title]');
    expect(title, 'title span rendered').toBeTruthy();
    act(() => title!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })));
    const input = host.querySelector('[data-rename-input]') as HTMLInputElement | null;
    expect(input, 'rename input appears').toBeTruthy();
    // jsdom + React controlled input: set value through the NATIVE setter so
    // React's onChange tracker sees the change.
    const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
    act(() => {
      nativeSetter.call(input!, 'new title');
      input!.dispatchEvent(new Event('input', { bubbles: true }));
    });
    await act(async () => {
      input!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await Promise.resolve();
    });
    expect(chatActions.updateSession).toHaveBeenCalledWith('s3', { title: 'new title' });
    expect((chatState.sessions as Array<{ title: string }>)[0].title).toBe('new title');
    unmount();
  });

  it('rename of an EMPTY or unchanged title is a no-op (no PATCH)', async () => {
    chatState.activeSessionId = 's4';
    chatState.sessions = [{ session_id: 's4', document_id: 'doc-2', reference_id: null, title: 'same' }];
    const host = mount();
    act(() => host.querySelector('[data-chat-title]')!.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })));
    const input = host.querySelector('[data-rename-input]') as HTMLInputElement;
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
      await Promise.resolve();
    });
    expect(chatActions.updateSession).not.toHaveBeenCalled();
    unmount();
  });
});
