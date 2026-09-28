/**
 * Unit tests for fetchContentBatch — the unified POST /api/documents/batch
 * client. Pure mapping via the
 * shared buildRefPreview rule: markdown items → { content }, image references →
 * { imageUrl }; an all-missing 404 collapses to {}.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

// Keep the real HttpError class (content-batch checks `err instanceof HttpError`)
// while overriding apiClient.post with a spy.
vi.mock('./client', async () => {
  const actual = await vi.importActual<typeof import('./client')>('./client');
  return { ...actual, apiClient: { post: vi.fn() } };
});

import { apiClient, HttpError } from './client';
import { fetchContentBatch } from './content-batch';

const mockPost = apiClient.post as ReturnType<typeof vi.fn>;

describe('fetchContentBatch', () => {
  beforeEach(() => {
    mockPost.mockReset();
  });

  it('maps a markdown item to { content } and an image reference to { imageUrl }', async () => {
    mockPost.mockResolvedValue({
      items: [
        { document_id: 'd1', content: 'doc body', media_type: 'markdown' },
        { document_id: 'r1', media_type: 'image', file_path: 'uploads/r1/pic.png' },
      ],
    });

    const out = await fetchContentBatch(['d1', 'r1']);

    expect(out.d1).toEqual({ content: 'doc body' });
    expect(out.r1).toEqual({ imageUrl: '/api/files/r1/pic.png' });
  });

  it('returns {} for an empty id list without issuing a POST', async () => {
    const out = await fetchContentBatch([]);
    expect(out).toEqual({});
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('POSTs the ids as { ids } to /documents/batch', async () => {
    mockPost.mockResolvedValue({ items: [] });
    await fetchContentBatch(['a', 'b']);
    expect(mockPost).toHaveBeenCalledWith('/documents/batch', { ids: ['a', 'b'] });
  });

  it('coalesces a markdown item with empty content to { content: "" }', async () => {
    mockPost.mockResolvedValue({ items: [{ document_id: 'd2', content: '', media_type: 'markdown' }] });
    const out = await fetchContentBatch(['d2']);
    expect(out.d2).toEqual({ content: '' });
  });

  it('maps an all-missing 404 to {} (uniform single-doc posture)', async () => {
    mockPost.mockRejectedValue(new HttpError(404, 'Documents not found'));
    const out = await fetchContentBatch(['gone']);
    expect(out).toEqual({});
  });

  it('re-throws non-404 errors so callers can fall back / surface them', async () => {
    mockPost.mockRejectedValue(new HttpError(500, 'boom'));
    await expect(fetchContentBatch(['x'])).rejects.toBeInstanceOf(HttpError);
  });
});
