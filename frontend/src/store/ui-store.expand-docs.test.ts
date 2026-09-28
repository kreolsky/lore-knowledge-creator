/**
 * Tests for the `expandDocs` sidebar-slice action (reveal-in-tree).
 *
 * INVARIANT pinned here: reveal ONLY expands — it removes ids from
 * collapsedDocIds, never adds. Idempotent + persisted through the same
 * saveUIStateNow path as toggleDocExpanded.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs } from './ui-store';

interface AppCtx {
  currentUser: { user_id: string } | null;
  currentProject: { project_id: string } | null;
  currentDocument: { document_id: string } | null;
}

beforeEach(() => {
  const _ctx: AppCtx = {
    currentUser: { user_id: 'u1' },
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  registerAppBridge({
    getAppContext: () => _ctx,
    showToast: () => {},
  });
  clearLastSavedBlobs();
  useUIStore.setState({ documents: {}, projectPrefsLoaded: true, collapsedDocIds: [] });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('expandDocs — reveal-in-tree expansion (only expands, never collapses)', () => {
  it('removes the given ancestors from collapsedDocIds', () => {
    useUIStore.setState({ collapsedDocIds: ['root', 'mid', 'unrelated'] });
    useUIStore.getState().expandDocs(['root', 'mid']);
    expect(useUIStore.getState().collapsedDocIds).toEqual(['unrelated']);
  });

  it('never collapses an already-expanded doc (idempotent, never adds)', () => {
    useUIStore.setState({ collapsedDocIds: ['collapsed-keep'] });
    // 'expanded-already' is NOT in the list — expandDocs must not add it.
    useUIStore.getState().expandDocs(['collapsed-keep', 'expanded-already']);
    expect(useUIStore.getState().collapsedDocIds).toEqual([]);
  });

  it('no-ops on an empty id list', () => {
    useUIStore.setState({ collapsedDocIds: ['a'] });
    useUIStore.getState().expandDocs([]);
    expect(useUIStore.getState().collapsedDocIds).toEqual(['a']);
  });

  it('persists the change through saveUIStateNow (PUT preferences)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true, status: 200, json: () => Promise.resolve({}),
    } as Response);

    useUIStore.setState({ collapsedDocIds: ['root', 'mid'] });
    useUIStore.getState().expandDocs(['root']);

    await vi.waitFor(() => {
      const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'));
      expect(putCall).toBeDefined();
    }, { timeout: 1000 });

    const putCall = fetchSpy.mock.calls.find(c => String(c[0]).includes('/api/preferences/p1'))!;
    const body = JSON.parse((putCall[1] as RequestInit).body as string);
    expect(body.preferences.collapsedDocIds).toEqual(['mid']);
  });
});
