/** Tests for per-document UI state slice — right panel tab/open + entity per (user × document). */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs, type EphemeralTab, type RememberedTab } from './ui-store';

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
  useUIStore.setState({ documents: {}, tabOverlay: null, projectPrefsLoaded: true, lastActiveChatSessionId: null });
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

describe('technical tabs — ephemeral overlay, never the stored tab', () => {
  // Every tab that is not chat/refs/notes is technical: shown as an overlay, never stored.
  // Record<EphemeralTab, …> binds the list to the union — a new technical tab cannot ship
  // without landing here.
  const TECHNICAL = Object.keys({
    search: true, settings: true, access: true, links: true, checkpoint: true, find: true,
  } as Record<EphemeralTab, true>) as EphemeralTab[];
  const okFetch = () => vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
  const prefCalls = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/'));

  // Seed a doc slice directly (no setter) so no debounced PUT timer is pending
  // when we start recording fetch calls.
  function seedDocTab(docId: string, tab: 'refs' | 'chat') {
    useUIStore.setState({
      documents: {
        [docId]: { mainEntity: { type: 'document', id: docId }, rightPanelOpen: true, rightPanelTab: tab },
      },
    });
  }

  it.each(TECHNICAL)('setRightPanelTab(docA, "%s") leaves the stored tab untouched, sets the overlay, fires no PUT', async (tab) => {
    const fetchSpy = okFetch();

    seedDocTab('docA', 'refs');
    fetchSpy.mockClear();

    useUIStore.getState().setRightPanelTab('docA', tab);

    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('refs');
    expect(useUIStore.getState().tabOverlay).toEqual({ docId: 'docA', tab });

    await new Promise(r => setTimeout(r, 300)); // > debounce window
    expect(prefCalls(fetchSpy)).toHaveLength(0);
  });

  it('selecting a remembered tab clears the overlay and persists that tab', async () => {
    const fetchSpy = okFetch();

    seedDocTab('docA', 'refs');
    useUIStore.getState().setRightPanelTab('docA', 'settings');
    useUIStore.getState().setRightPanelTab('docA', 'notes');

    expect(useUIStore.getState().tabOverlay).toBeNull();
    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe('notes');

    await vi.waitFor(() => {
      expect(prefCalls(fetchSpy).length).toBeGreaterThan(0);
    }, { timeout: 1000 });
    const putCall = prefCalls(fetchSpy).find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body.preferences.documents['docA'].rightPanelTab).toBe('notes');
  });

  it('setRightPanelOpen(docA, false) clears the overlay', () => {
    seedDocTab('docA', 'refs');
    useUIStore.getState().setRightPanelTab('docA', 'access');
    useUIStore.getState().setRightPanelOpen('docA', false);
    expect(useUIStore.getState().tabOverlay).toBeNull();
    expect(useUIStore.getState().documents['docA'].rightPanelOpen).toBe(false);
  });

  it.each([
    ['search', 'refs'], ['info', 'chat'], ['settings', 'chat'], ['access', 'chat'],
    ['links', 'chat'], ['checkpoint', 'chat'], ['find', 'chat'],
  ] as [string, RememberedTab][])('applyProjectPrefs repairs a persisted "%s" tab to "%s"', (legacy, repaired) => {
    useUIStore.getState().applyProjectPrefs({
      documents: {
        // A legacy server row: the stored tab predates the remembered/technical split.
        'docA': { mainEntity: { type: 'document', id: 'docA' }, rightPanelOpen: true, rightPanelTab: legacy as RememberedTab },
      },
    });
    expect(useUIStore.getState().documents['docA'].rightPanelTab).toBe(repaired);
  });

  it('resetForProjectSwitch clears the overlay', () => {
    useUIStore.getState().setRightPanelTab('docA', 'links');
    useUIStore.getState().resetForProjectSwitch(true);
    expect(useUIStore.getState().tabOverlay).toBeNull();
  });

  it.each(TECHNICAL)('orphan path: setRightPanelTab(null, "%s") flags ORPHAN_KEY, writes no doc slice, fires no PUT', async (tab) => {
    const fetchSpy = okFetch();
    fetchSpy.mockClear();

    useUIStore.getState().setRightPanelTab(null, tab);

    expect(useUIStore.getState().tabOverlay).toEqual({ docId: '__orphan__', tab });
    expect(useUIStore.getState().documents['__orphan__']).toBeUndefined();

    await new Promise(r => setTimeout(r, 300)); // > debounce window
    expect(prefCalls(fetchSpy)).toHaveLength(0);
  });
});

describe('removeDocState — strip per-doc slice on doc deletion', () => {

  it('is a no-op when docId is not present', () => {
    useUIStore.setState({ documents: {} });
    expect(() => useUIStore.getState().removeDocState('nonexistent')).not.toThrow();
    expect(useUIStore.getState().documents).toEqual({});
  });
});
