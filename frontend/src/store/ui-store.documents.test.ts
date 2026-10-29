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

  it('is a no-op when docId is not present', () => {
    useUIStore.setState({ documents: {} });
    expect(() => useUIStore.getState().removeDocState('nonexistent')).not.toThrow();
    expect(useUIStore.getState().documents).toEqual({});
  });
});
