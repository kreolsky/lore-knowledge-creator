/**
 * Unit tests for ChatHeader — ghost plaque, new-chat no-op, goto-parent
 * visibility rules (icon lives in the reserved LEFT slot), the always-reserved
 * title slot, double-click rename, the optimistic rename commit, and the
 * analyse-in-new-chat action (ghost + appendDraft prefill, unindexed guard).
 * Mounted with mocked stores per the repo's component-test pattern.
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
  draft: '',
};
const chatActions = {
  deleteSession: vi.fn(),
  updateSession: vi.fn().mockResolvedValue(undefined),
  loadModels: vi.fn(),
  startGhostChat: vi.fn(),
  // Real store semantics (misc-slice setDraft) so appendDraft's append-contract
  // is asserted against state, not against a bare spy.
  setDraft: vi.fn((v: string) => { chatState.draft = v; }),
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
// t mock carries the vars through so the prefill test pins WHICH key and
// WHICH params (title, chat_id) the request is composed with. Keys without
// vars still return the bare key, keeping the title^-prefix assertions below.
vi.mock('../../i18n', () => ({
  useTranslation: () => ({
    t: (k: string, vars?: Record<string, string | number>) => (vars ? `${k}:${JSON.stringify(vars)}` : k),
  }),
}));
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
  chatState.draft = '';
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

  it('ghost plaque offers no chat search — it lives in the Search tab', () => {
    const host = mount();
    expect(host.querySelector('svg.lucide-search')).toBeNull();
    expect(host.querySelector('[data-chat-search-input]')).toBeNull();
    unmount();
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

  it('ref session whose reference is OPEN shows the change-parent icon in the slot', () => {
    chatState.activeSessionId = 's2';
    chatState.sessions = [{ session_id: 's2', document_id: 'doc-1', reference_id: 'ref-9', reference_title: 'R' }];
    appState.currentReference = { reference_id: 'ref-9' };
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot, 'reserved slot rendered').toBeTruthy();
    expect(slot!.querySelector('[title="changeChatParent"]'), 'change-parent for ref session').toBeTruthy();
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'no goto-parent (ref is open)').toBeNull();
    unmount();
  });

  it('ref session under a DIFFERENT open reference shows goto-parent, not change-parent', () => {
    chatState.activeSessionId = 's2';
    chatState.sessions = [{ session_id: 's2', document_id: 'doc-1', reference_id: 'ref-9', reference_title: 'R' }];
    appState.currentReference = { reference_id: 'ref-other' };
    const host = mount();
    const slot = host.querySelector('[data-title-slot]');
    expect(slot!.querySelector('[title^="gotoParentDoc"]'), 'goto-parent').toBeTruthy();
    expect(slot!.querySelector('[title="changeChatParent"]'), 'no change-parent').toBeNull();
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

  it('analyse-in-new-chat opens a ghost, then appends the request after an EXISTING draft', () => {
    chatState.activeSessionId = 's5';
    chatState.sessions = [{ session_id: 's5', document_id: 'doc-2', reference_id: null, title: 'Diary', unindexed: false }];
    chatState.draft = 'existing note';
    const host = mount();
    const btn = host.querySelector('[title="analyseInNewChat"]');
    expect(btn, 'analyse affordance rendered').toBeTruthy();
    act(() => btn!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(chatActions.startGhostChat).toHaveBeenCalledTimes(1);
    // INVARIANT(data-loss) via appendDraft: the draft is one shared field —
    // the user's unsent text survives and the request is appended after it.
    expect(chatState.draft).toBe('existing note\n\nanalyseChatPrompt:{"title":"Diary","id":"s5"}');
    // The ghost opens BEFORE the prefill: startGhostChat clears the active
    // session, so the request must be composed from values captured first.
    expect(chatActions.setDraft.mock.invocationCallOrder[0]).toBeGreaterThan(
      chatActions.startGhostChat.mock.invocationCallOrder[0],
    );
    unmount();
  });

  it('analyse-in-new-chat prefills an EMPTY draft bare (no leading blanks)', () => {
    chatState.activeSessionId = 's6';
    chatState.sessions = [{ session_id: 's6', document_id: 'doc-2', reference_id: null, title: 'Notes', unindexed: false }];
    const host = mount();
    act(() => host.querySelector('[title="analyseInNewChat"]')!.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(chatState.draft).toBe('analyseChatPrompt:{"title":"Notes","id":"s6"}');
    unmount();
  });

  it('analyse-in-new-chat is disabled on an unindexed chat and does nothing on click', () => {
    chatState.activeSessionId = 's7';
    chatState.sessions = [{ session_id: 's7', document_id: 'doc-2', reference_id: null, title: 'Hidden', unindexed: true }];
    const host = mount();
    const btn = host.querySelector('[title="analyseChatHiddenHint"]') as HTMLButtonElement;
    expect(btn, 'disabled analyse affordance carries the unhide hint').toBeTruthy();
    expect(btn.disabled).toBe(true);
    // jsdom still dispatches click on a disabled button — the handler guard,
    // not the native disabled alone, keeps the ghost and draft untouched.
    act(() => btn.dispatchEvent(new MouseEvent('click', { bubbles: true })));
    expect(chatActions.startGhostChat).not.toHaveBeenCalled();
    expect(chatState.draft).toBe('');
    unmount();
  });
});
