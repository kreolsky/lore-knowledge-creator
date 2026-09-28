/**
 * Regression guard for "Chat with Reference".
 *
 * INVARIANT: the button ALWAYS opens a ref-scoped GHOST (activeSessionId = null)
 * with the reference attached, never auto-restoring a session or POSTing a row.
 * Why: the user decides whether to start fresh or pick an old chat from the list.
 * Existing chats are still loaded into the dropdown so they can open one manually. Existing chats are still loaded into the dropdown so the
 * user can open one manually. The ref is visible in the content picker and
 * transferGhostContext persists it onto the materialized row on the first send.
 *
 * The button and navigation previously diverged on "the open reference has no
 * own chats" (the regression axis that broke 3+ times). They now converge: the
 * button is always a deterministic ghost; the user decides old-vs-new.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

vi.mock('../events', () => ({
  emit: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
}));

vi.mock('./app-store', () => ({
  useAppStore: {
    getState: vi.fn(),
    subscribe: () => () => {},
  },
}));

vi.mock('./ui-store', () => ({
  useUIStore: {
    getState: vi.fn(),
  },
}));

vi.mock('../chat/context', () => ({
  setupChatContextBridge: vi.fn(),
  GHOST_SESSION_ID: '__ghost__',
  resolveCompletionContext: vi.fn(() => ({ document_ids: [], reference_ids: [] })),
  getContextForSession: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
  setContextForSession: vi.fn(),
  clearContextForSession: vi.fn(),
  getDerivedGhostContext: vi.fn(() => ({ docIds: [], refIds: [] })),
  resetGhostDeltas: vi.fn(),
  hydrateFromSessions: vi.fn(),
  pruneContext: vi.fn(),
  clearPendingContextPatches: vi.fn(),
  useChatContext: vi.fn(() => ({ documentIds: [], referenceIds: [] })),
}));

import { useChatStore } from './chat-store';
import type { Reference } from '../types';
// The tracker module is dep-free, so it is NOT mocked — imported directly so the
// proxy assertion (isChatScopeLoaded after openChatWithReference) exercises the
// real module-level state.
import { resetChatScopeTracker, isChatScopeLoaded } from '../chat/scope-tracker';

// Hoisted so individual tests can assert hydrateReference was called (the ref
// reaches the ghost context via currentReference — the derive base).
let hydrateReference: ReturnType<typeof vi.fn>;

function makeRef(overrides: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'ref-1',
    document_id: 'doc-1',
    project_id: 'proj-1',
    title: 'Test Reference',
    media_type: 'markdown' as const,
    source_url: null,
    content: '',
    processing_status: null,
    file_path: null,
    file_meta: null,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

function makeSession(overrides: Record<string, unknown> = {}) {
  return {
    session_id: 's1',
    project_id: 'proj-1',
    document_id: 'doc-1',
    reference_id: null as string | null,
    user_id: 'u1',
    title: '',
    model: 'm',
    system_prompt_id: null as string | null,
    context_ids: [] as string[],
    mode: 'ask' as const,
    created_at: '2025-01-01T00:00:00Z',
    updated_at: '2025-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(async () => {
  const { apiClient } = await import('../api/client');
  (apiClient.get as ReturnType<typeof vi.fn>).mockReset();
  (apiClient.post as ReturnType<typeof vi.fn>).mockReset();

  const { emit } = await import('../events');
  (emit as ReturnType<typeof vi.fn>).mockReset();

  const { useAppStore } = await import('./app-store');
  (useAppStore.getState as ReturnType<typeof vi.fn>).mockReset();
  hydrateReference = vi.fn().mockResolvedValue(null);
  (useAppStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    currentProject: { project_id: 'proj-1' },
    currentDocument: { document_id: 'doc-1' },
    currentReference: null,
    showToast: vi.fn(),
    setCurrentReference: vi.fn(),
    hydrateReference,
  });

  const { useUIStore } = await import('./ui-store');
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReset();
  (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
    documents: {},
    setLastActiveChatSession: vi.fn(),
    getLastActiveChatSession: vi.fn(() => null),
    getRefPreviewMode: vi.fn(() => false),
    getRefOpenMode: vi.fn(() => 'center'),
    setPendingForceAddRefId: vi.fn(),
    setRightPanelTab: vi.fn(),
  });

  useChatStore.getState().reset();
  useChatStore.setState({
    models: [],
    modelsLoaded: false,
    defaultModel: '',
    pendingInputFocus: false,
  });
  resetChatScopeTracker();
});

// Shared: every scenario MUST land on a ghost (the ghost DERIVES its context from
// the open reference via currentReference — no attach call), with no session
// created and the panel revealed.
async function expectGhostOnRef() {
  expect(useChatStore.getState().activeSessionId).toBeNull();
  const { apiClient } = await import('../api/client');
  const createCalls = (apiClient.post as ReturnType<typeof vi.fn>).mock.calls
    .filter((c: string[]) => c[0] === '/chat/sessions');
  expect(createCalls).toHaveLength(0);
}

// ─── Scenario 1: existing ref session present → STILL a ghost (no restore) ──

describe('openChatWithReference — existing session', () => {
  it('does NOT restore the existing session; lands on a ref-scoped ghost', async () => {
    const { apiClient } = await import('../api/client');
    const { useUIStore } = await import('./ui-store');
    const { emit } = await import('../events');

    const ref = makeRef();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'existing-s', reference_id: ref.reference_id }),
    ]);

    const setRightPanelTab = vi.fn();
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => null),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      setRightPanelTab,
    });

    await useChatStore.getState().openChatWithReference(ref);

    await expectGhostOnRef();
    expect(setRightPanelTab).toHaveBeenCalledWith('doc-1', 'chat');
    expect(emit).toHaveBeenCalledWith('open-right-panel');
    // The existing session is still in the dropdown list.
    expect(useChatStore.getState().sessions.some(s => s.session_id === 'existing-s')).toBe(true);
  });
});

// ─── Scenario 2: no sessions → ref-scoped ghost (derived context) ─────────

describe('openChatWithReference — no sessions', () => {
  it('opens a ref-scoped ghost (no POST); marks the ref as a context source', async () => {
    const { apiClient } = await import('../api/client');
    const { useAppStore } = await import('./app-store');
    const ref = makeRef();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    await useChatStore.getState().openChatWithReference(ref);

    await expectGhostOnRef();
    // The ref reaches the ghost context via currentReference: the derive base is
    // UNCONDITIONAL on the open entity (see context.ts / ghost-context.ts), and
    // hydrateReference is what commits the ref as currentReference. (The old
    // write-only setTalkToDocument flag is gone — proven unnecessary here.)
    expect(hydrateReference).toHaveBeenCalledWith(ref);
  });
});

// ─── Scenario 2b: parent doc has chats, ref has none → ghost, no fallback ──

describe('openChatWithReference — parent doc has chats, ref has none', () => {
  it('stays a ref-scoped ghost; does NOT fall back to the parent doc chat', async () => {
    const { apiClient } = await import('../api/client');
    const { useUIStore } = await import('./ui-store');
    const ref = makeRef();
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'doc-chat', reference_id: null }),
    ]);
    // Parent doc has a saved chat pin — must NOT be activated.
    (useUIStore.getState as ReturnType<typeof vi.fn>).mockReturnValue({
      documents: {},
      setLastActiveChatSession: vi.fn(),
      getLastActiveChatSession: vi.fn(() => 'doc-chat'),
      getRefPreviewMode: vi.fn(() => false),
      getRefOpenMode: vi.fn(() => 'center'),
      setRightPanelTab: vi.fn(),
    });

    await useChatStore.getState().openChatWithReference(ref);

    await expectGhostOnRef();
    expect(useChatStore.getState().activeSessionId).not.toBe('doc-chat');
    // PROXY: the marker is what stops the ChatPanel mount effect from re-running
    // loadSessions (which the resolver would turn into a restore_saved → 'doc-chat').
    // The store test never mounts ChatPanel, so this asserts the marker is SET, not
    // that a mounted effect actually skips — the manual first-open check covers that.
    expect(isChatScopeLoaded('proj-1')).toBe(true);
  });
});

// ─── Scenario 6: switch between references → ghost each time ───────────

describe('openChatWithReference — switch between refs', () => {
  it('opens a ref-2 ghost (does not keep ref-1 session)', async () => {
    const { apiClient } = await import('../api/client');
    const ref2 = makeRef({ reference_id: 'ref-2' });
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValue([
      makeSession({ session_id: 'ref1-session', reference_id: 'ref-1' }),
    ]);

    useChatStore.setState({
      sessions: [makeSession({ session_id: 'ref1-session', reference_id: 'ref-1' })],
      activeSessionId: 'ref1-session',
      documentId: 'doc-1',
    });

    await useChatStore.getState().openChatWithReference(ref2);

    expect(useChatStore.getState().activeSessionId).toBeNull();
    // The NEW ref (ref-2) is what reaches the ghost context: hydrateReference
    // commits it as currentReference, which IS the derive base. Asserted as the
    // LAST call — nothing after the switch may re-hydrate the prior ref-1 (whose
    // session is still in the store) and pull it back into the base.
    expect(hydrateReference).toHaveBeenLastCalledWith(ref2);
  });
});
