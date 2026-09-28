/**
 * Ghost-pin for "Work with Selection".
 *
 * Covers: (1) the in-memory ghost region lifecycle (set/clear/reset), (2) startAgentChat
 * attaches the pin to the ZERO/GHOST chat WITHOUT creating a row and gates on full access,
 * (3) createSession writes the pinned region SYNCHRONOUSLY (before its ghost-context awaits)
 * so it is present when the highlight annotation fires — the Part A ordering regression.
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { create } from 'zustand';

const postMock = vi.fn<(...args: unknown[]) => Promise<unknown>>();
let accessLevel = 'full';
const showToastMock = vi.fn();
vi.mock('../../api/client', () => ({
  apiClient: {
    post: (...args: unknown[]) => postMock(...args),
    patch: vi.fn(() => Promise.resolve({})),
  },
}));
vi.mock('../app-store', () => ({
  useAppStore: Object.assign(() => ({}), {
    getState: () => ({
      accessLevel,
      showToast: showToastMock,
      currentProject: { project_id: 'proj-1' },
      currentDocument: { document_id: 'doc-1' },
      currentReference: null,
    }),
  }),
}));
// Partial mock: real ui-store exports stay live (new exports used by prod code
// cannot break this mock) — only the store instance is replaced.
vi.mock('../ui-store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../ui-store')>();
  return {
    ...actual,
    useUIStore: {
      getState: () => ({
        getLastActiveChatSession: () => null,
        setLastActiveChatSession: vi.fn(),
        setRightPanelTab: vi.fn(),
        getRefOpenMode: () => 'center',
      }),
    },
  };
});
vi.mock('../../events', () => ({ emit: vi.fn() }));
vi.mock('../../i18n', () => ({ t: (k: string) => k }));
// The ghost-context bridge is orthogonal here — stub the reads createSession makes.
vi.mock('../../chat/context', () => ({
  hydrateFromSessions: vi.fn(),
  clearContextForSession: vi.fn(),
  setContextForSession: vi.fn(),
  addItemToContext: vi.fn(() => Promise.resolve()),
  getDerivedGhostContext: () => ({ docIds: [], refIds: [] }),
  ghostBaseTargets: () => ({ docs: [], refs: [] }),
  resetGhostDeltas: vi.fn(),
}));

import { createSessionsSlice } from './sessions-slice';
import { createMiscSlice } from './misc-slice';
import { getPendingRegion, clearPendingRegion } from './pending-selection';
import type { ChatState } from './types';
import type { PinnedRegion } from '../../types';

const REGION: PinnedRegion = { doc_id: 'doc-1', relFrom: { a: 1 }, relTo: { a: 2 } };

function buildStore() {
  return create<ChatState>((set, get) => ({
    sessions: [],
    activeSessionId: null,
    ghostRegion: null,
    ghostAgentAuto: false,
    ghostSystemPromptId: null,
    ghostModel: '',
    messages: [],
    streaming: null,
    ...createSessionsSlice(set, get),
    ...createMiscSlice(set, get),
  } as unknown as ChatState));
}

beforeEach(() => {
  vi.clearAllMocks();
  accessLevel = 'full';
});

describe('ghost region lifecycle', () => {
  it('setGhostRegion / clearGhostRegion mutate in-memory state', () => {
    const store = buildStore();
    store.getState().setGhostRegion(REGION);
    expect(store.getState().ghostRegion).toEqual(REGION);
    store.getState().clearGhostRegion();
    expect(store.getState().ghostRegion).toBeNull();
  });

  it('reset() clears the ghost region', () => {
    const store = buildStore();
    store.getState().setGhostRegion(REGION);
    store.getState().reset();
    expect(store.getState().ghostRegion).toBeNull();
  });
});

describe('startAgentChat — ghost attach (no row) + access gate', () => {
  it('full access: attaches region to the ghost, switches to agent, creates NO session row', async () => {
    const store = buildStore();
    await store.getState().startAgentChat({
      doc_id: 'doc-1', relFrom: REGION.relFrom, relTo: REGION.relTo, from_cp: 0, to_cp: 3, text: 'abc',
    });
    expect(postMock).not.toHaveBeenCalled();          // no materialization
    expect(store.getState().activeSessionId).toBeNull(); // still the ghost
    expect(store.getState().ghostRegion).toEqual(REGION);
  });

  it('non-full access: no-op (no ghost region, toast shown)', async () => {
    accessLevel = 'viewer';
    const store = buildStore();
    await store.getState().startAgentChat({
      doc_id: 'doc-1', relFrom: REGION.relFrom, relTo: REGION.relTo, from_cp: 0, to_cp: 3, text: 'abc',
    });
    expect(store.getState().ghostRegion).toBeNull();
    expect(showToastMock).toHaveBeenCalledWith('agentModeRequiresAccess', 'error');
  });
});

describe('createSession — pinned region written synchronously after activation (Part A ordering)', () => {
  // The regression moved setPendingRegion AFTER createSession's
  // awaits, so the region was empty when the activation-triggered `regionChanged`
  // highlight annotation fired → the blue highlight never appeared. The fix keeps
  // setPendingRegion synchronous, right after insertAndPinSession and before any await.
  it('the region is present BEFORE createSession yields again after activation', async () => {
    const store = buildStore();
    postMock.mockResolvedValue({
      session_id: 'real-1', document_id: 'doc-1', reference_id: null, user_id: 'u1',
      title: null, model: 'm', system_prompt_id: null, context_ids: [],
      updated_at: '2026-07-10T00:00:00Z', last_message_at: null,
    });

    // Detect the ordering via a microtask queued from the activation listener. The
    // listener fires synchronously inside insertAndPinSession's set(); the microtask it
    // queues runs AFTER the rest of createSession's synchronous block (so setPendingRegion
    // has run in the fixed code) but BEFORE createSession resumes from its cascade await
    // (so the buggy "after the await" version still sees null here). This is the only
    // observation point that distinguishes the fix from the regression — a plain
    // post-await assertion cannot, because both versions set the region before returning.
    let regionAtMidpoint: PinnedRegion | null | undefined;
    const unsub = store.subscribe((state, prev) => {
      if (prev.activeSessionId !== 'real-1' && state.activeSessionId === 'real-1') {
        queueMicrotask(() => { regionAtMidpoint = getPendingRegion('real-1'); });
      }
    });

    try {
      const created = await store.getState().createSession({
        projectId: 'proj-1', documentId: 'doc-1', targetDocId: 'doc-1',
        hasRegion: true, region: REGION, focus: true,
      });
      expect(created?.session_id).toBe('real-1');
    } finally {
      unsub();
    }

    expect(regionAtMidpoint, 'region must be written before createSession yields after activation').toEqual(REGION);
    expect(getPendingRegion('real-1')).toEqual(REGION);
    clearPendingRegion('real-1');
  });
});
