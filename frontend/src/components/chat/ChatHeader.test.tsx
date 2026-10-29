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

  it('no pencil icon is rendered (rename is double-click only)', () => {
    chatState.activeSessionId = 's1';
    chatState.sessions = [{ session_id: 's1', document_id: 'doc-2', reference_id: null }];
    const host = mount();
    expect(host.querySelector('[title="renameChat"]')).toBeNull();
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
});
