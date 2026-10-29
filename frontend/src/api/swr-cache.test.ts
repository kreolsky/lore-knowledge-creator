/**
 * swr-cache — the resource-cache primitive: bounded LRU + in-flight dedup (fetch =
 * cache-then-network, revalidate = always-revalidate) + generation-guarded clear.
 * Pure logic (no network — loaders are injected).
 */
import { describe, it, expect, vi } from 'vitest';
import { createResourceCache } from './swr-cache';

describe('createResourceCache', () => {

  it('evicts the in-flight slot on rejection and does not cache the failure', async () => {
    const c = createResourceCache<string>();
    await expect(c.fetch('a', () => Promise.reject(new Error('net')))).rejects.toThrow('net');
    expect(c.get('a')).toBeUndefined(); // a rejection is never written as a value
    const retry = vi.fn(() => Promise.resolve('v'));
    await c.fetch('a', retry);
    expect(retry).toHaveBeenCalledTimes(1); // slot was freed on rejection → retry re-fetches
  });
});

describe('createResourceCache.revalidate', () => {

  it('frees the slot on settle AND rejection; a rejection is never cached', async () => {
    const c = createResourceCache<string[]>();
    await expect(c.revalidate('scope', () => Promise.reject(new Error('net')))).rejects.toThrow('net');
    expect(c.get('scope')).toBeUndefined();
    const retry = vi.fn(() => Promise.resolve(['v']));
    await c.revalidate('scope', retry);
    expect(retry).toHaveBeenCalledTimes(1); // slot freed on rejection → retry refetches

    c.invalidate('scope');
    const again = vi.fn(() => Promise.resolve(['v2']));
    await c.revalidate('scope', again);     // slot freed on settle too
    expect(again).toHaveBeenCalledTimes(1);
  });
});
