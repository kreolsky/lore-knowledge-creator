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
import { clearPreviewCache, fetchDocumentContent } from './useDocumentPreview';
import { setPublicFileContext } from '../utils/reference-url';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

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

  it('REJECTS on a 404 for an out-of-scope public doc (no silent empty)', async () => {
    setPublicFileContext('lore_pub');
    mockGet.mockRejectedValueOnce(new Error('404'));
    await expect(fetchDocumentContent('d-oos')).rejects.toThrow('404');
  });
});
