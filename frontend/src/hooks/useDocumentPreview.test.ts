/** Tests for useDocumentPreview fetch contract (failure, public-share path, logout
 *  + project-switch lifecycle, dedup, seed) on the resource-cache primitive. The old
 *  white-box FIFO/size tests died with the module's hand-rolled map (plan:
 *  resource-cache-one-primitive) — LRU order is pinned by the primitive's own tests
 *  in src/api/swr-cache.test.ts. */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from '../api/client';
import { clearPreviewCache, fetchDocumentContent, seedDocPreview } from './useDocumentPreview';
import { setPublicFileContext } from '../utils/reference-url';
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

describe('fetchDocumentContent failure is an error, never an empty body', () => {
  beforeEach(() => {
    clearPreviewCache();
    mockGet.mockReset();
  });

  it('REJECTS when apiClient.get rejects', async () => {
    mockGet.mockRejectedValue(new Error('network'));
    await expect(fetchDocumentContent('d1')).rejects.toThrow('network');
  });

  it('does not cache the failure — a second call re-fetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('network'));
    await expect(fetchDocumentContent('d2')).rejects.toThrow('network');

    mockGet.mockResolvedValueOnce({ content: 'recovered' });
    await expect(fetchDocumentContent('d2')).resolves.toBe('recovered');
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('a genuinely-empty doc still resolves "" and is cached (not retried)', async () => {
    mockGet.mockResolvedValue({ content: '' });
    await expect(fetchDocumentContent('d3')).resolves.toBe('');
    await expect(fetchDocumentContent('d3')).resolves.toBe('');
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

describe('fetchDocumentContent public-share path', () => {
  beforeEach(() => {
    clearPreviewCache();
    mockGet.mockReset();
    setPublicFileContext(null);
  });
  afterEach(() => {
    setPublicFileContext(null);
  });

  it('hits the authed /documents/{id} endpoint when no public token is set', async () => {
    mockGet.mockResolvedValueOnce({ content: 'authed body' });
    await expect(fetchDocumentContent('d-auth')).resolves.toBe('authed body');
    expect(mockGet).toHaveBeenCalledWith('/documents/d-auth');
  });

  it('hits the public document-keyed endpoint when a public context is set', async () => {
    setPublicFileContext('root-doc-pub');
    mockGet.mockResolvedValueOnce({ content: 'public body' });
    await expect(fetchDocumentContent('d-pub')).resolves.toBe('public body');
    // WHY document-keyed (plan "public-document-ids"): the funnel is keyed on
    // the document uuid, not a token. The authed endpoint 401s anonymous callers
    // and the apiClient redirect-on-401 guard kicks the visitor off /docs/:id.
    expect(mockGet).toHaveBeenCalledWith('/public/documents/d-pub');
  });

  it('response shape is identical ({ content }) — no special parsing needed', async () => {
    setPublicFileContext('lore_pub');
    mockGet.mockResolvedValueOnce({ content: '# Heading\nbody' });
    await expect(fetchDocumentContent('d-shape')).resolves.toBe('# Heading\nbody');
  });

  it('caches public-path fetches the same way (LRU + dedup)', async () => {
    setPublicFileContext('lore_pub');
    mockGet.mockResolvedValueOnce({ content: 'cached body' });
    await fetchDocumentContent('d-cache');
    // Second call: cache hit, no new fetch.
    await expect(fetchDocumentContent('d-cache')).resolves.toBe('cached body');
    expect(mockGet).toHaveBeenCalledTimes(1);
  });

  it('REJECTS on a 404 for an out-of-scope public doc (no silent empty)', async () => {
    setPublicFileContext('lore_pub');
    mockGet.mockRejectedValueOnce(new Error('404'));
    await expect(fetchDocumentContent('d-oos')).rejects.toThrow('404');
  });
});

describe('useDocumentPreview logout lifecycle', () => {
  beforeEach(() => {
    clearPreviewCache();
    mockGet.mockReset();
    setPublicFileContext(null);
  });

  it('a soft logout drops the cached body: a post-logout call re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ content: 'user A body' });
    await expect(fetchDocumentContent('d1')).resolves.toBe('user A body');
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Soft logout: setCurrentUser(null) fires every registered clear
    // (SYSTEM: logout-handlers). The prior user's bodies must not survive it.
    clearUserScopedCaches();

    mockGet.mockResolvedValueOnce({ content: 'user B body' });
    await expect(fetchDocumentContent('d1')).resolves.toBe('user B body');
    expect(mockGet).toHaveBeenCalledTimes(2); // d1 was NOT served from A's cache
  });

  it('a fetch resolving after the logout writes nothing to the cache', async () => {
    let resolve!: (v: { content: string }) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));
    const p = fetchDocumentContent('d2');
    clearUserScopedCaches(); // logout lands before the body arrives
    resolve({ content: 'late user A body' });
    await expect(p).resolves.toBe('late user A body'); // the caller keeps its value

    mockGet.mockResolvedValueOnce({ content: 'fresh' });
    await expect(fetchDocumentContent('d2')).resolves.toBe('fresh'); // cache stayed empty → GET
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

describe('useDocumentPreview dedup + seed', () => {
  beforeEach(() => {
    clearPreviewCache();
    mockGet.mockReset();
    setPublicFileContext(null);
  });

  it('a second concurrent call shares ONE GET', async () => {
    let resolve!: (v: { content: string }) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));
    const p1 = fetchDocumentContent('d3');
    const p2 = fetchDocumentContent('d3');
    expect(mockGet).toHaveBeenCalledTimes(1); // collapsed to one in-flight GET
    resolve({ content: 'body' });
    await expect(p1).resolves.toBe('body');
    await expect(p2).resolves.toBe('body');
  });

  it('a seeded body is served without a GET (transclusion batch)', async () => {
    seedDocPreview('d4', 'seeded body');
    await expect(fetchDocumentContent('d4')).resolves.toBe('seeded body');
    expect(mockGet).not.toHaveBeenCalled();
  });
});

describe('useDocumentPreview project-switch lifecycle', () => {
  beforeEach(() => {
    clearPreviewCache();
    mockGet.mockReset();
    setPublicFileContext(null);
    useAppStore.setState({ currentProject: makeProject('proj-A'), currentUser: null });
  });

  it('a project switch drops the cached body: the next hover re-fetches', async () => {
    mockGet.mockResolvedValueOnce({ content: 'proj A body' });
    await expect(fetchDocumentContent('d5')).resolves.toBe('proj A body');
    expect(mockGet).toHaveBeenCalledTimes(1);

    // Real project-switch entry: setCurrentProject → resetSiblingStores →
    // resetEditorHost({scope:'project'}) → clearPreviewCache (editor-host.ts).
    useAppStore.getState().setCurrentProject(makeProject('proj-B'));

    mockGet.mockResolvedValueOnce({ content: 'proj B body' });
    await expect(fetchDocumentContent('d5')).resolves.toBe('proj B body');
    expect(mockGet).toHaveBeenCalledTimes(2); // d5 was NOT served across the switch
  });
});
