/** Search mode memory — the last document mode survives a chats round and a reload,
 * and the Search tab's starting mode is picked from the tab it is opened from. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs } from './ui-store';
import { searchModeOnOpen } from './ui-store/search-slice';

function projectPuts(mock: { mock: { calls: unknown[][] } }): { preferences: Record<string, unknown> }[] {
  return mock.mock.calls
    .filter(c => String(c[0]).includes('/api/preferences/p1'))
    .map(c => JSON.parse((c[1] as { body: string }).body) as { preferences: Record<string, unknown> });
}

beforeEach(() => {
  registerAppBridge({
    getAppContext: () => ({ currentUser: { user_id: 'u1' }, currentProject: { project_id: 'p1' }, currentDocument: null }),
    showToast: () => {},
  });
  clearLastSavedBlobs();
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
  useUIStore.setState({ projectPrefsLoaded: true, searchMode: 'fulltext', searchDocMode: 'fulltext' });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('searchDocMode', () => {
  it('a chats round keeps the last document mode', () => {
    useUIStore.getState().setSearchMode('semantic');
    useUIStore.getState().setSearchMode('chats');
    expect(useUIStore.getState().searchMode).toBe('chats');
    expect(useUIStore.getState().searchDocMode).toBe('semantic');
  });

  it('rides the persisted blob and a reload restores it', async () => {
    const fetchSpy = vi.mocked(globalThis.fetch);
    useUIStore.getState().setSearchMode('semantic');
    useUIStore.getState().setSearchMode('chats');
    await vi.waitFor(() => { expect(projectPuts(fetchSpy)).not.toHaveLength(0); }, { timeout: 1000 });
    const blob = projectPuts(fetchSpy).at(-1)!.preferences;
    expect(blob.searchDocMode).toBe('semantic');

    useUIStore.setState({ searchMode: 'fulltext', searchDocMode: 'fulltext' });
    useUIStore.getState().applyProjectPrefs(blob);
    expect(useUIStore.getState().searchMode).toBe('chats');
    expect(useUIStore.getState().searchDocMode).toBe('semantic');
  });
});

describe('searchModeOnOpen', () => {
  it('from the Chat tab ⇒ chats', () => {
    expect(searchModeOnOpen('chat', 'semantic', 'semantic')).toBe('chats');
  });
  it('from another tab after a chats round ⇒ the last document mode', () => {
    expect(searchModeOnOpen('refs', 'chats', 'semantic')).toBe('semantic');
  });
  it('from a closed panel (no visible tab) ⇒ never chats', () => {
    expect(searchModeOnOpen(null, 'fulltext', 'fulltext')).toBeNull();
    expect(searchModeOnOpen(null, 'chats', 'semantic')).toBe('semantic');
  });
  it('from another tab in a document mode ⇒ unchanged', () => {
    expect(searchModeOnOpen('refs', 'fulltext', 'fulltext')).toBeNull();
  });
});
