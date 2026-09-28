/**
 * swr-cache — the resource-cache primitive: bounded LRU + in-flight dedup (fetch =
 * cache-then-network, revalidate = always-revalidate) + generation-guarded clear.
 * Pure logic (no network — loaders are injected).
 */
import { describe, it, expect, vi } from 'vitest';
import { createResourceCache } from './swr-cache';

/** Minimal deferred so in-flight timing is observable without fake timers. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('createResourceCache', () => {
  it('returns undefined for an unknown key', () => {
    const c = createResourceCache<number[]>();
    expect(c.get('missing')).toBeUndefined();
  });

  it('returns the last seeded value for a key', () => {
    const c = createResourceCache<number[]>();
    c.seed('a', [1, 2]);
    expect(c.get('a')).toEqual([1, 2]);
    c.seed('a', [3]);
    expect(c.get('a')).toEqual([3]);
  });

  it('invalidate drops a single key', () => {
    const c = createResourceCache<number[]>();
    c.seed('a', [1]);
    c.seed('b', [2]);
    c.invalidate('a');
    expect(c.get('a')).toBeUndefined();
    expect(c.get('b')).toEqual([2]);
  });

  it('clear drops everything', () => {
    const c = createResourceCache<number[]>();
    c.seed('a', [1]);
    c.seed('b', [2]);
    c.clear();
    expect(c.get('a')).toBeUndefined();
    expect(c.get('b')).toBeUndefined();
  });

  it('evicts the least-recently-used entry past the bound', () => {
    const c = createResourceCache<number>(2);
    c.seed('a', 1);
    c.seed('b', 2);
    c.seed('c', 3); // evicts 'a' (oldest)
    expect(c.get('a')).toBeUndefined();
    expect(c.get('b')).toBe(2);
    expect(c.get('c')).toBe(3);
  });

  it('get bumps recency so the touched key survives eviction', () => {
    const c = createResourceCache<number>(2);
    c.seed('a', 1);
    c.seed('b', 2);
    c.get('a');       // 'a' now most-recent
    c.seed('c', 3);   // evicts 'b' (now oldest), not 'a'
    expect(c.get('a')).toBe(1);
    expect(c.get('b')).toBeUndefined();
    expect(c.get('c')).toBe(3);
  });

  it('dedups concurrent same-key fetches into ONE load and ONE promise', async () => {
    const c = createResourceCache<string>();
    const d = deferred<string>();
    const load = vi.fn(() => d.promise);
    const p1 = c.fetch('a', load);
    const p2 = c.fetch('a', load);
    expect(load).toHaveBeenCalledTimes(1);
    expect(p2).toBe(p1); // the second caller shares the exact in-flight promise
    d.resolve('v');
    expect(await p1).toBe('v');
    expect(await p2).toBe('v');
  });

  it('evicts the in-flight slot on settle: a later fetch re-loads after invalidate', async () => {
    const c = createResourceCache<string>();
    await c.fetch('a', () => Promise.resolve('one'));
    expect(c.get('a')).toBe('one');
    c.invalidate('a');
    const reload = vi.fn(() => Promise.resolve('two'));
    const out = await c.fetch('a', reload);
    expect(reload).toHaveBeenCalledTimes(1); // slot was freed on settle
    expect(out).toBe('two');
  });

  it('evicts the in-flight slot on rejection and does not cache the failure', async () => {
    const c = createResourceCache<string>();
    await expect(c.fetch('a', () => Promise.reject(new Error('net')))).rejects.toThrow('net');
    expect(c.get('a')).toBeUndefined(); // a rejection is never written as a value
    const retry = vi.fn(() => Promise.resolve('v'));
    await c.fetch('a', retry);
    expect(retry).toHaveBeenCalledTimes(1); // slot was freed on rejection → retry re-fetches
  });

  it('seed serves a later fetch without a GET', async () => {
    const c = createResourceCache<string>();
    c.seed('doc1', 'body');
    const load = vi.fn(() => Promise.resolve('fetched'));
    const out = await c.fetch('doc1', load);
    expect(load).not.toHaveBeenCalled();
    expect(out).toBe('body');
  });

  it('evicts least-recently-used across seed, get and fetch hits', async () => {
    const c = createResourceCache<string>(2);
    c.seed('a', '1');
    c.seed('b', '2');
    expect(c.get('a')).toBe('1');   // 'a' is now most-recent
    c.seed('c', '3');               // evicts 'b'
    expect(c.get('b')).toBeUndefined();
    expect(c.get('a')).toBe('1');
    expect(c.get('c')).toBe('3');
  });

  it('a fetch hit skips the load and bumps recency', async () => {
    const c = createResourceCache<string>(2);
    await c.fetch('a', () => Promise.resolve('1'));
    await c.fetch('b', () => Promise.resolve('2'));
    const hitLoad = vi.fn(() => Promise.resolve('x'));
    await c.fetch('a', hitLoad);    // cache hit: no load, 'a' most-recent
    expect(hitLoad).not.toHaveBeenCalled();
    c.seed('c', '3');               // evicts 'b'
    expect(c.get('b')).toBeUndefined();
    expect(c.get('a')).toBe('1');
  });

  it('a fetch settling after clear() writes nothing to the cache', async () => {
    const c = createResourceCache<string>();
    const d = deferred<string>();
    const p = c.fetch('a', () => d.promise);
    c.clear();                      // logout/project switch lands mid-flight
    d.resolve('stale');
    await expect(p).resolves.toBe('stale'); // the original caller keeps its value
    expect(c.get('a')).toBeUndefined();     // but nothing repopulates the cache
  });

  it('a post-clear fetch starts its own load instead of sharing the pre-clear one', async () => {
    const c = createResourceCache<string>();
    const d1 = deferred<string>();
    const first = c.fetch('a', () => d1.promise);
    c.clear();
    const fresh = vi.fn(() => Promise.resolve('fresh'));
    const p2 = c.fetch('a', fresh);
    expect(fresh).toHaveBeenCalledTimes(1);  // no share with the pre-clear slot
    d1.resolve('old-user');
    await expect(first).resolves.toBe('old-user');
    await expect(p2).resolves.toBe('fresh');
  });
});

describe('createResourceCache.revalidate', () => {
  it('dedups concurrent same-key loads into ONE load and ONE promise', async () => {
    const c = createResourceCache<string[]>();
    const d = deferred<string[]>();
    const load = vi.fn(() => d.promise);
    const p1 = c.revalidate('scope', load);
    const p2 = c.revalidate('scope', load);
    expect(load).toHaveBeenCalledTimes(1);
    expect(p2).toBe(p1);
    d.resolve(['v']);
    expect(await p1).toEqual(['v']);
    expect(await p2).toEqual(['v']);
  });

  it('NEVER serves the cache: a seeded value does not skip the load (always-revalidate)', async () => {
    const c = createResourceCache<string[]>();
    c.seed('scope', ['stale']);
    const load = vi.fn(() => Promise.resolve(['fresh']));
    const out = await c.revalidate('scope', load);
    expect(load).toHaveBeenCalledTimes(1);  // the fetch is the correctness backstop
    expect(out).toEqual(['fresh']);
  });

  it('writes the settled value: a later get reads it', async () => {
    const c = createResourceCache<string[]>();
    await c.revalidate('scope', () => Promise.resolve(['v1']));
    expect(c.get('scope')).toEqual(['v1']);
  });

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

  it('a revalidate settling after clear() writes nothing to the cache', async () => {
    const c = createResourceCache<string[]>();
    const d = deferred<string[]>();
    const p = c.revalidate('scope', () => d.promise);
    c.clear();                                // logout lands mid-flight
    d.resolve(['stale']);
    await expect(p).resolves.toEqual(['stale']); // the caller keeps its value
    expect(c.get('scope')).toBeUndefined();      // but the cache stayed empty
  });
});

describe('createResourceCache.invalidate — the in-flight window', () => {
  it('a fetch settling after invalidate() writes nothing to the cache', async () => {
    const c = createResourceCache<string>();
    const d = deferred<string>();
    const p = c.fetch('ref1', () => d.promise);
    c.invalidate('ref1');                       // WS reference_update lands mid-flight
    d.resolve('pre-edit body');
    await expect(p).resolves.toBe('pre-edit body'); // the original caller keeps its value
    expect(c.get('ref1')).toBeUndefined();          // but the edited-away body is not re-seated
  });

  it('a revalidate settling after invalidate() writes nothing to the cache', async () => {
    const c = createResourceCache<string[]>();
    const d = deferred<string[]>();
    const p = c.revalidate('scope', () => d.promise);
    c.invalidate('scope');
    d.resolve(['pre-edit']);
    await expect(p).resolves.toEqual(['pre-edit']);
    expect(c.get('scope')).toBeUndefined();
  });

  it('a post-invalidate caller starts its own load instead of sharing the pre-invalidate one', async () => {
    const c = createResourceCache<string>();
    const d1 = deferred<string>();
    const first = c.fetch('ref1', () => d1.promise);
    c.invalidate('ref1');
    const fresh = vi.fn(() => Promise.resolve('post-edit body'));
    const second = c.fetch('ref1', fresh);
    expect(fresh).toHaveBeenCalledTimes(1);      // no share with the pre-invalidate slot
    d1.resolve('pre-edit body');
    await expect(first).resolves.toBe('pre-edit body');
    await expect(second).resolves.toBe('post-edit body');
    expect(c.get('ref1')).toBe('post-edit body'); // the fresh load's write still lands
  });

  it('invalidating key A does NOT refuse the settle write of in-flight key B', async () => {
    const c = createResourceCache<string>();
    const dA = deferred<string>();
    const dB = deferred<string>();
    const pA = c.fetch('a', () => dA.promise);
    const pB = c.fetch('b', () => dB.promise);
    c.invalidate('a');                          // a single-entry eviction, not a cache-wide stall
    dA.resolve('a-body');
    dB.resolve('b-body');
    await Promise.all([pA, pB]);
    expect(c.get('a')).toBeUndefined();
    expect(c.get('b')).toBe('b-body');
  });

  it('a later invalidate does not refuse a load that started after it', async () => {
    const c = createResourceCache<string>();
    c.invalidate('ref1');                       // an invalidate for a key with nothing in flight
    await c.fetch('ref1', () => Promise.resolve('body'));
    expect(c.get('ref1')).toBe('body');          // the next load is unaffected
  });
});
