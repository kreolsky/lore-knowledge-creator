/** Behaviour matrix for useLastMessagePreview on the resource-cache primitive:
 * in-flight dedup, cache-hit-without-GET, logout + project-switch lifecycle. The old
 * white-box FIFO/size tests died with the module's hand-rolled map (plan:
 * resource-cache-one-primitive) — LRU order is pinned by the primitive's own tests
 * in src/api/swr-cache.test.ts. This cache has NO seed entry (transclusion never
 * seeds last-message previews); the cache-hit test is its serve-without-GET proof. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from '../api/client';
import { clearLastMessagePreviewCache, fetchLastMessagePreview } from './useLastMessagePreview';
import { clearUserScopedCaches } from '../store/logout-handlers';
import { useAppStore } from '../store/app-store';
import type { Project } from '../types';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

function makeProject(project_id: string): Project {
  return {
    project_id,
    name: project_id,
    status: 'active',
    project_context: '',
    index_doc_id: null,
    voice_recording_doc_id: null,
    last_accessed_doc_id: null,
    owner_id: null,
    is_public: false,
    my_access: 'full',
    created_at: '2024-01-01T00:00:00Z',
  };
}

describe('useLastMessagePreview dedup + cache hit', () => {
  beforeEach(() => {
    clearLastMessagePreviewCache();
    mockGet.mockReset();
  });

  it('a second concurrent call shares ONE GET', async () => {
    let resolve!: (v: { preview: string }) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));
    const p1 = fetchLastMessagePreview('s3');
    const p2 = fetchLastMessagePreview('s3');
    expect(mockGet).toHaveBeenCalledTimes(1); // collapsed to one in-flight GET
    resolve({ preview: 'preview' });
    await expect(p1).resolves.toBe('preview');
    await expect(p2).resolves.toBe('preview');
  });

  it('a settled body is served as a cache hit without a second GET', async () => {
    mockGet.mockResolvedValueOnce({ preview: 'cached' });
    await expect(fetchLastMessagePreview('s4')).resolves.toBe('cached');
    await expect(fetchLastMessagePreview('s4')).resolves.toBe('cached');
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe('useLastMessagePreview logout lifecycle', () => {
  beforeEach(() => {
    clearLastMessagePreviewCache();
    mockGet.mockReset();
  });

  it('a soft logout drops the cached body: a post-logout call re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ preview: 'user A preview' });
    await expect(fetchLastMessagePreview('s1')).resolves.toBe('user A preview');
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Soft logout: setCurrentUser(null) fires every registered clear
    // (see SYSTEM: logout-handlers). The prior user's bodies must not survive it.
    clearUserScopedCaches();

    mockGet.mockResolvedValueOnce({ preview: 'user B preview' });
    await expect(fetchLastMessagePreview('s1')).resolves.toBe('user B preview');
    expect(mockGet).toHaveBeenCalledTimes(2); // s1 was NOT served from A's cache
  });

  it('a fetch resolving after the logout writes nothing to the cache', async () => {
    let resolve!: (v: { preview: string }) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));
    const p = fetchLastMessagePreview('s2');
    clearUserScopedCaches(); // logout lands before the body arrives
    resolve({ preview: 'late user A preview' });
    await expect(p).resolves.toBe('late user A preview'); // the caller keeps its value

    mockGet.mockResolvedValueOnce({ preview: 'fresh' });
    await expect(fetchLastMessagePreview('s2')).resolves.toBe('fresh'); // cache stayed empty → GET
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

describe('useLastMessagePreview project-switch lifecycle', () => {
  beforeEach(() => {
    clearLastMessagePreviewCache();
    mockGet.mockReset();
    useAppStore.setState({ currentProject: makeProject('proj-A'), currentUser: null });
  });

  it('a project switch drops the cached body: the next hover re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ preview: 'proj A preview' });
    await expect(fetchLastMessagePreview('s5')).resolves.toBe('proj A preview');
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Real project-switch entry: setCurrentProject → resetSiblingStores →
    // clearLastMessagePreviewCache (app-store.ts).
    useAppStore.getState().setCurrentProject(makeProject('proj-B'));

    mockGet.mockResolvedValueOnce({ preview: 'proj B preview' });
    await expect(fetchLastMessagePreview('s5')).resolves.toBe('proj B preview');
    expect(mockGet).toHaveBeenCalledTimes(2); // s5 was NOT served across the switch
  });
});
