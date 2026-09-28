/** Tests for per-document UI state slice — right panel tab/open + entity per (user × document). */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs } from './ui-store';

interface AppCtx {
  currentUser: { user_id: string } | null;
  currentProject: { project_id: string } | null;
  currentDocument: { document_id: string } | null;
}

let _ctx: AppCtx = { currentUser: null, currentProject: null, currentDocument: null };
function setCtx(patch: Partial<AppCtx>) { _ctx = { ..._ctx, ...patch }; }

beforeEach(() => {
  _ctx = {
    currentUser: { user_id: 'u1' },
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  registerAppBridge({
    getAppContext: () => _ctx,
    showToast: () => {},
  });
  clearLastSavedBlobs();
  // projectPrefsLoaded gates triggerSaveUI — emulate the normal hydrated runtime.
  useUIStore.setState({ documents: {}, projectPrefsLoaded: true, lastActiveChatSessionId: null });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('per-document UI state', () => {
  it('setRightPanelTab(docA) does not affect docB', () => {
    const s = useUIStore.getState();
    s.setRightPanelTab('docA', 'chat');
    s.setRightPanelTab('docB', 'notes');
    const docs = useUIStore.getState().documents;
    expect(docs['docA'].rightPanelTab).toBe('chat');
    expect(docs['docB'].rightPanelTab).toBe('notes');
  });

  it('setRightPanelOpen is per-document', () => {
    const s = useUIStore.getState();
    s.setRightPanelOpen('docA', false);
    s.setRightPanelOpen('docB', true);
    const docs = useUIStore.getState().documents;
    expect(docs['docA'].rightPanelOpen).toBe(false);
    expect(docs['docB'].rightPanelOpen).toBe(true);
  });

  it('setLastActiveChatSession writes the project-level active chat', () => {
    useUIStore.getState().setLastActiveChatSession('sess-X');
    expect(useUIStore.getState().getLastActiveChatSession()).toBe('sess-X');
  });

  it('getLastActiveChatSession returns null when never set', () => {
    expect(useUIStore.getState().getLastActiveChatSession()).toBeNull();
  });

  it('setLastActiveChatSession(null) clears the project-level active chat', () => {
    useUIStore.getState().setLastActiveChatSession('sess-X');
    useUIStore.getState().setLastActiveChatSession(null);
    expect(useUIStore.getState().getLastActiveChatSession()).toBeNull();
  });

  it('setCurrentReferenceForDoc writes into documents[docId].selectedReferenceId', () => {
    useUIStore.getState().setCurrentReferenceForDoc('docA', 'ref-1');
    expect(useUIStore.getState().documents['docA'].selectedReferenceId).toBe('ref-1');
  });

  it('setMainEntity stores main-area entity per document', () => {
    useUIStore.getState().setMainEntity('docA', { type: 'reference', id: 'ref-99' });
    expect(useUIStore.getState().documents['docA'].mainEntity).toEqual({ type: 'reference', id: 'ref-99' });
  });

  it('useActiveDocState returns slice for current document', () => {
    useUIStore.getState().setRightPanelTab('docA', 'chat');
    setCtx({ currentDocument: { document_id: 'docA' } });
    const slice = useUIStore.getState().getActiveDocState();
    expect(slice.rightPanelTab).toBe('chat');
  });

  it('useActiveDocState returns DEFAULTS for unknown doc', () => {
    setCtx({ currentDocument: { document_id: 'unknown' } });
    const slice = useUIStore.getState().getActiveDocState();
    expect(slice.rightPanelTab).toBe('chat');
    expect(slice.rightPanelOpen).toBe(true);
  });

  it('useActiveDocState returns ephemeral DEFAULTS when no active doc', () => {
    setCtx({ currentDocument: null });
    const slice = useUIStore.getState().getActiveDocState();
    expect(slice).toBeDefined();
    expect(slice.rightPanelTab).toBe('chat');
  });
});

describe('applyProjectPrefs — clean-slate migration', () => {
  it('seeds documents map + project-level active chat from server prefs', () => {
    useUIStore.getState().applyProjectPrefs({
      lastActiveChatSessionId: 'saved-chat',
      documents: {
        'docA': { mainEntity: { type: 'document', id: 'docA' }, rightPanelOpen: true, rightPanelTab: 'chat' },
      },
    });
    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('chat');
    expect(useUIStore.getState().getLastActiveChatSession()).toBe('saved-chat');
  });

  it('ignores legacy top-level rightPanelTab and rightPanelOpen', () => {
    const legacyBlob = {
      rightPanelTab: 'chat',
      rightPanelOpen: false,
      chatSessionByDoc: { 'docA': 'old-session' },
    } as unknown as Parameters<ReturnType<typeof useUIStore.getState>['applyProjectPrefs']>[0];
    useUIStore.getState().applyProjectPrefs(legacyBlob);
    expect(useUIStore.getState().documents).toEqual({});
  });

  it('handles missing documents field', () => {
    useUIStore.getState().applyProjectPrefs({});
    expect(useUIStore.getState().documents).toEqual({});
  });
});

describe('buildUIBlob via PUT — emits new shape only', () => {
  it('PUT body contains documents map, no legacy keys', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    useUIStore.getState().setRightPanelTab('docA', 'chat');
    useUIStore.getState().setLastActiveChatSession('sess-X');

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });

    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    const prefs = body.preferences;
    expect(prefs.documents).toBeDefined();
    expect(prefs.documents['docA'].rightPanelTab).toBe('chat');
    expect(prefs.lastActiveChatSessionId).toBe('sess-X');
    expect(prefs.rightPanelTab).toBeUndefined();
    expect(prefs.rightPanelOpen).toBeUndefined();
    expect(prefs.chatSessionByDoc).toBeUndefined();
    expect(prefs.currentReferenceByDoc).toBeUndefined();
    expect(prefs.refPreviewMode).toBeUndefined();
  });
});

describe('resetForProjectSwitch — clears documents map', () => {
  it('clears the per-doc slice on project switch', () => {
    useUIStore.getState().setRightPanelTab('docA', 'chat');
    expect(Object.keys(useUIStore.getState().documents)).toHaveLength(1);
    useUIStore.getState().resetForProjectSwitch(true);
    expect(useUIStore.getState().documents).toEqual({});
  });
});

describe('preferences fetch dedup + hydration flag', () => {
  // Counts fetch calls hitting urlPart that are NOT PUTs (i.e. the GET loads).
  const prefGets = (spy: ReturnType<typeof vi.spyOn>, urlPart: string) =>
    spy.mock.calls.filter((c: unknown[]) =>
      String(c[0]).includes(urlPart) && (c[1] as RequestInit | undefined)?.method !== 'PUT',
    ).length;

  it('loadProjectPrefs dedups concurrent calls for the same project', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);
    fetchSpy.mockClear();

    useUIStore.getState().loadProjectPrefs('u1', 'p1');
    useUIStore.getState().loadProjectPrefs('u1', 'p1');
    await useUIStore.getState().awaitProjectPrefs('p1');

    expect(prefGets(fetchSpy, '/api/preferences/p1')).toBe(1);
  });

  it('loadGlobalPrefs dedups concurrent calls', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);
    fetchSpy.mockClear();

    const p1 = useUIStore.getState().loadGlobalPrefs();
    const p2 = useUIStore.getState().loadGlobalPrefs();
    await Promise.all([p1, p2]);

    expect(prefGets(fetchSpy, '/api/preferences/_global')).toBe(1);
  });

  it('projectPrefsLoaded flips true after load and resets false on project switch', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);
    useUIStore.setState({ projectPrefsLoaded: false });

    useUIStore.getState().loadProjectPrefs('u1', 'p1');
    await useUIStore.getState().awaitProjectPrefs('p1');
    expect(useUIStore.getState().projectPrefsLoaded).toBe(true);

    useUIStore.getState().resetForProjectSwitch(true);
    expect(useUIStore.getState().projectPrefsLoaded).toBe(false);
  });
});

describe('orphan slice — ephemeral, not persisted', () => {
  it('setRightPanelTab(null, ...) does NOT trigger PUT /api/preferences', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);
    fetchSpy.mockClear();

    useUIStore.getState().setRightPanelTab(null, 'chat');
    useUIStore.getState().setRightPanelOpen(null, false);

    await new Promise(r => setTimeout(r, 300)); // > debounce window
    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/'));
    expect(putCall).toBeUndefined();
  });

  it('stripOrphan: PUT body excludes documents["__orphan__"]', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    // Manually inject an orphan slice + a real per-doc slice into state.
    useUIStore.setState({
      documents: {
        '__orphan__': { mainEntity: { type: 'document', id: '' }, rightPanelOpen: false, rightPanelTab: 'search' },
        'docA': { mainEntity: { type: 'document', id: 'docA' }, rightPanelOpen: true, rightPanelTab: 'chat' },
      },
    });
    fetchSpy.mockClear();
    // Trigger any persist-action.
    useUIStore.getState().setSidebarTab('toc');

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });

    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body.preferences.documents['docA']).toBeDefined();
    expect(body.preferences.documents['__orphan__']).toBeUndefined();
  });
});

describe('search tab — ephemeral overlay, never the stored tab', () => {
  // Seed a doc slice directly (no setter) so no debounced PUT timer is pending
  // when we start recording fetch calls.
  function seedDocTab(docId: string, tab: 'refs' | 'chat') {
    useUIStore.setState({
      documents: {
        [docId]: { mainEntity: { type: 'document', id: docId }, rightPanelOpen: true, rightPanelTab: tab },
      },
    });
  }

  it('setRightPanelTab(docA, "search") leaves the stored tab untouched, sets the overlay flag, fires no PUT', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    seedDocTab('docA', 'refs');
    fetchSpy.mockClear();

    useUIStore.getState().setRightPanelTab('docA', 'search');

    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('refs');
    expect(useUIStore.getState().searchTabDocId).toBe('docA');

    await new Promise(r => setTimeout(r, 300)); // > debounce window
    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/'));
    expect(putCall).toBeUndefined();
  });

  it('selecting another tab clears the overlay and persists that tab', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    seedDocTab('docA', 'refs');
    useUIStore.getState().setRightPanelTab('docA', 'search');
    useUIStore.getState().setRightPanelTab('docA', 'notes');

    expect(useUIStore.getState().searchTabDocId).toBeNull();
    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('notes');

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });
    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body.preferences.documents['docA'].rightPanelTab).toBe('notes');
  });

  it('setRightPanelOpen(docA, false) clears the overlay', () => {
    seedDocTab('docA', 'refs');
    useUIStore.getState().setRightPanelTab('docA', 'search');
    useUIStore.getState().setRightPanelOpen('docA', false);
    expect(useUIStore.getState().searchTabDocId).toBeNull();
    expect(useUIStore.getState().documents['docA'].rightPanelOpen).toBe(false);
  });

  it('applyProjectPrefs migrates a persisted "search" tab to "refs"', () => {
    useUIStore.getState().applyProjectPrefs({
      documents: {
        'docA': { mainEntity: { type: 'document', id: 'docA' }, rightPanelOpen: true, rightPanelTab: 'search' },
      },
    });
    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('refs');
  });

  it('resetForProjectSwitch clears the overlay', () => {
    useUIStore.getState().setRightPanelTab('docA', 'search');
    useUIStore.getState().resetForProjectSwitch(true);
    expect(useUIStore.getState().searchTabDocId).toBeNull();
  });

  it('orphan path: setRightPanelTab(null, "search") flags ORPHAN_KEY, writes no doc slice, fires no PUT', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);
    fetchSpy.mockClear();

    useUIStore.getState().setRightPanelTab(null, 'search');

    expect(useUIStore.getState().searchTabDocId).toBe('__orphan__');
    expect(useUIStore.getState().documents['__orphan__']).toBeUndefined();

    await new Promise(r => setTimeout(r, 300)); // > debounce window
    expect(fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/'))).toBeUndefined();
  });
});

describe('removeDocState — strip per-doc slice on doc deletion', () => {
  it('removes the slice for given docId', () => {
    useUIStore.setState({
      documents: {
        'docA': { mainEntity: { type: 'document', id: 'docA' }, rightPanelOpen: true, rightPanelTab: 'chat' },
        'docB': { mainEntity: { type: 'document', id: 'docB' }, rightPanelOpen: true, rightPanelTab: 'notes' },
      },
    });
    useUIStore.getState().removeDocState('docA');
    expect(useUIStore.getState().documents['docA']).toBeUndefined();
    expect(useUIStore.getState().documents['docB']).toBeDefined();
  });

  it('is a no-op when docId is not present', () => {
    useUIStore.setState({ documents: {} });
    expect(() => useUIStore.getState().removeDocState('nonexistent')).not.toThrow();
    expect(useUIStore.getState().documents).toEqual({});
  });
});

describe('project-level active chat — overwrite + clear', () => {
  it('setLastActiveChatSession overwrites the previous value', () => {
    useUIStore.getState().setLastActiveChatSession('sess-old');
    useUIStore.getState().setLastActiveChatSession('sess-new');
    expect(useUIStore.getState().getLastActiveChatSession()).toBe('sess-new');
  });

  it('clears to null', () => {
    useUIStore.getState().setLastActiveChatSession('sess-A');
    useUIStore.getState().setLastActiveChatSession(null);
    expect(useUIStore.getState().getLastActiveChatSession()).toBeNull();
  });
});
