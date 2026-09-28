/** Admin section prefs on the global-prefs slice — per-user persistence contract.
 *
 * Pins:
 * - setAdminSectionTab PUTs /api/preferences/_global and the blob (built
 *   field-by-field in triggerSaveGlobalPrefs) carries the field — a missed
 *   field there silently drops persistence;
 * - loadGlobalPrefs hydrates it from the server row;
 * - default is null (page falls back to Users).
 *
 * The section aside WIDTH is deliberately NOT a section field — it is
 * panelWidths.left, shared with the project sidebar (see
 * hooks/useSectionAsideWidth.ts); its persistence is covered by the existing
 * setPanelWidth tests and AdminPage.test.tsx.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs } from './ui-store';

let _ctx = {
  currentUser: { user_id: 'u1' } as { user_id: string } | null,
  currentProject: { project_id: 'p1' } as { project_id: string } | null,
  currentDocument: null as { document_id: string } | null,
};

/** apiClient PUTs ride fetch(url, {body: JSON.stringify({preferences})}) —
 * the payload is the RequestInit's .body string, not the second mock arg. */
function globalPuts(mock: { mock: { calls: unknown[][] } }): { preferences: Record<string, unknown> }[] {
  return mock.mock.calls
    .filter(c => String(c[0]).includes('/api/preferences/_global'))
    .map(c => JSON.parse((c[1] as { body: string }).body) as { preferences: Record<string, unknown> });
}

beforeEach(() => {
  _ctx = { currentUser: { user_id: 'u1' }, currentProject: { project_id: 'p1' }, currentDocument: null };
  registerAppBridge({
    getAppContext: () => _ctx,
    showToast: () => {},
  });
  clearLastSavedBlobs();
  // Absent prefs fields come back as {} — the store must keep defaults then.
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
  useUIStore.setState({ adminSectionTab: null });
});

describe('admin section prefs — setters persist on _global', () => {
  it('setAdminSectionTab PUTs _global with adminSectionTab in the blob', async () => {
    const fetchSpy = vi.mocked(globalThis.fetch);
    useUIStore.getState().setAdminSectionTab('projects');

    await vi.waitFor(() => { expect(globalPuts(fetchSpy)).not.toHaveLength(0); }, { timeout: 1000 });
    const last = globalPuts(fetchSpy).at(-1)!.preferences;
    expect(last.adminSectionTab).toBe('projects');
    // The pre-existing global fields still ride along — blob is field-by-field.
    expect(last.theme).toBeDefined();
    expect(last.language).toBeDefined();
  });
});

describe('admin section prefs — loadGlobalPrefs hydration', () => {
  it('hydrates adminSectionTab from the server row', async () => {
    vi.mocked(globalThis.fetch).mockResolvedValue({
      ok: true, status: 200,
      json: () => Promise.resolve({ adminSectionTab: 'embeddings' }),
    } as Response);

    await useUIStore.getState().loadGlobalPrefs();

    expect(useUIStore.getState().adminSectionTab).toBe('embeddings');
  });

  it('empty prefs row keeps the null default (page falls back to Users)', async () => {
    await useUIStore.getState().loadGlobalPrefs();

    expect(useUIStore.getState().adminSectionTab).toBeNull();
  });
});
