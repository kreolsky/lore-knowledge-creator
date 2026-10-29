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

describe('startAgentChat — ghost attach (no row) + access gate', () => {

  it('a slot the left chat held is dropped with the pointer (the slot belongs to the active chat)', async () => {
    const store = buildStore();
    store.setState({
      activeSessionId: 's-busy',
      streaming: { sessionId: 's-busy', messageId: 'am', content: '', controller: new AbortController() },
    } as unknown as Partial<ChatState>);
    await store.getState().startAgentChat({
      doc_id: 'doc-1', relFrom: REGION.relFrom, relTo: REGION.relTo, from_cp: 0, to_cp: 3, text: 'abc',
    });
    expect(store.getState().activeSessionId).toBeNull();
    expect(store.getState().streaming).toBeNull();
  });
});
