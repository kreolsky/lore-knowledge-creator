/**
 * Tests for the public-share guard on global-prefs persistence.
 *
 * Context: anonymous visitors on /s/:token have no server-side identity. The four
 * global-prefs setters (setTheme / setLanguage / setPanelWidth / setSplitRatio)
 * all route through triggerSaveGlobalPrefs → PUT /api/preferences/_global, which
 * is auth-gated and returns 401; handleResponse turns 401 into a redirect to '/',
 * kicking the visitor off the share page. The guard skips the PUT entirely when
 * isPublicShare is true — theme/language/widths still apply in-session.
 */

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
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
  // Reset public-share flag between tests so order doesn't matter.
  useUIStore.setState({ isPublicShare: false });
});

describe('public share — global-prefs PUT is skipped', () => {
  it('setTheme does not PUT when isPublicShare is true', () => {
    useUIStore.setState({ isPublicShare: true });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    useUIStore.getState().setTheme('dark');

    const globalPuts = fetchSpy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/_global'));
    expect(globalPuts).toHaveLength(0);
  });

  it('setLanguage does not PUT when isPublicShare is true', () => {
    useUIStore.setState({ isPublicShare: true });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    useUIStore.getState().setLanguage('ru');

    const globalPuts = fetchSpy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/_global'));
    expect(globalPuts).toHaveLength(0);
  });

  it('setPanelWidth does not PUT when isPublicShare is true', () => {
    useUIStore.setState({ isPublicShare: true });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    useUIStore.getState().setPanelWidth('left', 300);

    const globalPuts = fetchSpy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/_global'));
    expect(globalPuts).toHaveLength(0);
  });

  it('setSplitRatio does not PUT when isPublicShare is true', () => {
    useUIStore.setState({ isPublicShare: true });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    useUIStore.getState().setSplitRatio(0.7);

    const globalPuts = fetchSpy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/_global'));
    expect(globalPuts).toHaveLength(0);
  });
});

describe('publicFallbackDocIds — user-scoped refusal memory', () => {
  it('clearUserScopedCaches() (the soft-logout chokepoint) empties the set', async () => {
    const { clearUserScopedCaches } = await import('./logout-handlers');
    useUIStore.getState().markPublicFallback(['d-1', 'd-2']);
    expect(useUIStore.getState().publicFallbackDocIds.size).toBe(2);

    clearUserScopedCaches();

    // A same-tab re-login can be a project member: a stale id would serve them
    // the read-only public view instead of the editor.
    expect(useUIStore.getState().publicFallbackDocIds.size).toBe(0);
  });

  it('markPublicFallback accumulates ids without clobbering earlier ones', () => {
    useUIStore.getState().markPublicFallback(['d-1']);
    useUIStore.getState().markPublicFallback(['d-2', 'd-1']);
    const ids = useUIStore.getState().publicFallbackDocIds;
    expect(ids.has('d-1') && ids.has('d-2')).toBe(true);
    useUIStore.setState({ publicFallbackDocIds: new Set() });
  });
});

describe('authed mode — global-prefs PUT still fires (regression guard)', () => {
  it('setTheme PUTs to /api/preferences/_global when isPublicShare is false', async () => {
    useUIStore.setState({ isPublicShare: false });
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    useUIStore.getState().setTheme('dark');

    await vi.waitFor(() => {
      const globalPuts = fetchSpy.mock.calls.filter(c => String(c[0]).includes('/api/preferences/_global'));
      expect(globalPuts.length).toBeGreaterThanOrEqual(1);
    }, { timeout: 1000 });
  });
});

describe('public share — in-session state still applies', () => {
  it('setTheme updates the store even when the PUT is skipped', () => {
    useUIStore.setState({ isPublicShare: true });
    useUIStore.getState().setTheme('dark');
    expect(useUIStore.getState().theme).toBe('dark');
  });

  it('setLanguage updates the store even when the PUT is skipped', () => {
    useUIStore.setState({ isPublicShare: true });
    useUIStore.getState().setLanguage('ru');
    expect(useUIStore.getState().language).toBe('ru');
  });
});
