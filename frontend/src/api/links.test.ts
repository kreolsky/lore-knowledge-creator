/** Link first-circle cache: fresh-fetch bypass + invalidation. */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from './client';
import {
  fetchDocumentLinks,
  fetchDocumentLinksFresh,
  invalidateLinkCache,
  clearLinkCache,
  subscribeLinkCache,
  getLinkCacheVersion,
} from './links';

const get = vi.mocked(apiClient.get);

describe('links cache', () => {
  beforeEach(() => {
    clearLinkCache();
    get.mockReset();
  });

  it('cached fetch hits the network once, then serves the warm entry', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    await fetchDocumentLinks('x');
    await fetchDocumentLinks('x');
    expect(get).toHaveBeenCalledTimes(1);
  });

  it('fresh fetch always re-hits the network and refreshes the cache', async () => {
    get.mockResolvedValueOnce({ document_ids: [], reference_ids: [] });
    await fetchDocumentLinks('x'); // warm with empty
    get.mockResolvedValueOnce({ document_ids: ['d2'], reference_ids: [] });
    const fresh = await fetchDocumentLinksFresh('x');
    expect(get).toHaveBeenCalledTimes(2);
    expect(fresh.document_ids).toEqual(['d2']);
    // cache was refreshed by the fresh call
    const warm = await fetchDocumentLinks('x');
    expect(warm.document_ids).toEqual(['d2']);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it('invalidateLinkCache forces the next cached fetch back to the network', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    await fetchDocumentLinks('x');
    invalidateLinkCache('doc', 'x');
    await fetchDocumentLinks('x');
    expect(get).toHaveBeenCalledTimes(2);
  });
});

describe('links cache version', () => {
  beforeEach(() => {
    clearLinkCache();
    get.mockReset();
  });

  it('bumps the version + notifies subscribers on a fetch that writes the cache', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    const before = getLinkCacheVersion();
    const cb = vi.fn();
    const unsub = subscribeLinkCache(cb);
    await fetchDocumentLinks('x');
    expect(getLinkCacheVersion()).toBeGreaterThan(before);
    expect(cb).toHaveBeenCalled();
    unsub();
  });

  it('does NOT bump on a warm cache hit (no mutation)', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    await fetchDocumentLinks('x'); // warms
    const after = getLinkCacheVersion();
    const cb = vi.fn();
    const unsub = subscribeLinkCache(cb);
    await fetchDocumentLinks('x'); // hit, no set
    expect(getLinkCacheVersion()).toBe(after);
    expect(cb).not.toHaveBeenCalled();
    unsub();
  });

  it('bumps on invalidate and on clear', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    await fetchDocumentLinks('x');
    const v1 = getLinkCacheVersion();
    invalidateLinkCache('doc', 'x');
    const v2 = getLinkCacheVersion();
    expect(v2).toBeGreaterThan(v1);
    await fetchDocumentLinks('y'); // re-warm so clear has something to drop
    const v3 = getLinkCacheVersion();
    clearLinkCache();
    expect(getLinkCacheVersion()).toBeGreaterThan(v3);
  });

  it('stops notifying after unsubscribe', async () => {
    get.mockResolvedValue({ document_ids: ['d1'], reference_ids: [] });
    const cb = vi.fn();
    const unsub = subscribeLinkCache(cb);
    unsub();
    await fetchDocumentLinks('x');
    expect(cb).not.toHaveBeenCalled();
  });
});
