/**
 * Behaviour matrix for fetchRefPreview on the resource-cache primitive: in-flight
 * dedup (same contract as loadReferences), failure contract, logout + project-switch
 * lifecycle, seed-without-GET.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from '../api/client';
import { fetchRefPreview, clearRefPreviewCache, seedRefPreview } from './useReferencePreview';
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

describe('fetchRefPreview in-flight dedup', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
  });

  it('collapses two concurrent same-id calls into ONE apiClient.get', async () => {
    let resolve!: (v: unknown) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));

    const p1 = fetchRefPreview('r1');
    const p2 = fetchRefPreview('r1');

    expect(mockGet).toHaveBeenCalledTimes(1);
    resolve({ reference_id: 'r1', media_type: 'markdown', content: 'body' });
    await expect(p1).resolves.toEqual({ content: 'body' });
    await expect(p2).resolves.toEqual({ content: 'body' });
  });

  it('issues a second get for a different id (distinct in-flight key)', async () => {
    mockGet.mockImplementation((endpoint: string) =>
      Promise.resolve({ reference_id: endpoint.split('/').pop(), media_type: 'markdown', content: 'x' }),
    );
    await fetchRefPreview('r2');
    await fetchRefPreview('r3');
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('is a cache hit (no GET) once the in-flight settles', async () => {
    mockGet.mockResolvedValue({ reference_id: 'r4', media_type: 'markdown', content: 'cached' });
    await fetchRefPreview('r4');
    await fetchRefPreview('r4');
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe('fetchRefPreview failure is an error, never an empty body', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
  });

  it('REJECTS when apiClient.get rejects, instead of resolving {content: ""}', async () => {
    mockGet.mockRejectedValue(new Error('network'));
    await expect(fetchRefPreview('r5')).rejects.toThrow('network');
  });

  it('does not cache the failure — a second call re-fetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('network'));
    await expect(fetchRefPreview('r6')).rejects.toThrow('network');

    mockGet.mockResolvedValueOnce({ reference_id: 'r6', media_type: 'markdown', content: 'recovered' });
    await expect(fetchRefPreview('r6')).resolves.toEqual({ content: 'recovered' });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('a genuinely-empty ref still resolves "" and is cached (not retried)', async () => {
    mockGet.mockResolvedValue({ reference_id: 'r7', media_type: 'markdown', content: '' });
    await expect(fetchRefPreview('r7')).resolves.toEqual({ content: '' });
    await expect(fetchRefPreview('r7')).resolves.toEqual({ content: '' });
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe('useReferencePreview logout lifecycle', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
  });

  it('a soft logout drops the cached body: a post-logout call re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ reference_id: 'r10', media_type: 'markdown', content: 'user A body' });
    await expect(fetchRefPreview('r10')).resolves.toEqual({ content: 'user A body' });
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Soft logout: setCurrentUser(null) fires every registered clear
    // (SYSTEM: logout-handlers). The prior user's bodies must not survive it.
    clearUserScopedCaches();

    mockGet.mockResolvedValueOnce({ reference_id: 'r10', media_type: 'markdown', content: 'user B body' });
    await expect(fetchRefPreview('r10')).resolves.toEqual({ content: 'user B body' });
    expect(mockGet).toHaveBeenCalledTimes(2); // r10 was NOT served from A's cache
  });

  it('a fetch resolving after the logout writes nothing to the cache', async () => {
    let resolve!: (v: { reference_id: string; media_type: string; content: string }) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));
    const p = fetchRefPreview('r11');
    clearUserScopedCaches(); // logout lands before the body arrives
    resolve({ reference_id: 'r11', media_type: 'markdown', content: 'late user A body' });
    await expect(p).resolves.toEqual({ content: 'late user A body' }); // the caller keeps its value

    mockGet.mockResolvedValueOnce({ reference_id: 'r11', media_type: 'markdown', content: 'fresh' });
    await expect(fetchRefPreview('r11')).resolves.toEqual({ content: 'fresh' }); // cache stayed empty → GET
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

describe('useReferencePreview seed', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
  });

  it('a seeded body is served without a GET (transclusion batch)', async () => {
    seedRefPreview('r20', { content: 'seeded body' });
    await expect(fetchRefPreview('r20')).resolves.toEqual({ content: 'seeded body' });
    expect(mockGet).not.toHaveBeenCalled();
  });
});

describe('useReferencePreview project-switch lifecycle', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
    useAppStore.setState({ currentProject: makeProject('proj-A'), currentUser: null });
  });

  it('a project switch drops the cached body: the next hover re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ reference_id: 'r21', media_type: 'markdown', content: 'proj A body' });
    await expect(fetchRefPreview('r21')).resolves.toEqual({ content: 'proj A body' });
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Real project-switch entry: setCurrentProject → resetSiblingStores →
    // resetEditorHost({scope:'project'}) → clearRefPreviewCache (editor-host.ts).
    useAppStore.getState().setCurrentProject(makeProject('proj-B'));

    mockGet.mockResolvedValueOnce({ reference_id: 'r21', media_type: 'markdown', content: 'proj B body' });
    await expect(fetchRefPreview('r21')).resolves.toEqual({ content: 'proj B body' });
    expect(mockGet).toHaveBeenCalledTimes(2); // r21 was NOT served across the switch
  });
});
