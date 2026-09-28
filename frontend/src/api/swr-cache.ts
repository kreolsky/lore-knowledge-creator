/**
 * SYSTEM: swr-cache — resource-cache primitive (bounded LRU + in-flight dedup)
 * for network-backed caches: panel list fetches (references, chat/note sessions)
 * and id/scope-keyed body fetches (document/reference/last-message previews).
 *
 * ARCH: `createResourceCache` is the ONE cache construction — it owns the LRU
 * map, the in-flight dedup and the settle guard, so a consumer declares its
 * key, its loader and its lifecycle instead of hand-rolling another copy of
 * `MAX_CACHE_SIZE` + `evictIfNeeded` + an in-flight Map. Surface: LRU + in-flight
 * dedup (`fetch` / `revalidate`) + `seed` + `invalidate` + `clear`. `seed` writes
 * a value WITHOUT a fetch (the transclusion batch seeds bodies it already holds,
 * so a later hover-preview of a batched id is a free cache hit).
 *
 * ARCH: two fetch policies over one dedup. `fetch` is cache-then-network — a hit
 * resolves the cached value without calling the loader (hover previews are
 * fetch-on-miss). `revalidate` is always-revalidate — the load ALWAYS runs and
 * the cached value is a PRE-gate paint the caller reads via `get`, never a
 * terminal; a WS event missed while the scope was closed self-heals on the next
 * open. Both share the in-flight slot, the slot-guarded settle write and
 * the free-on-settle/reject slot eviction.
 *
 * ARCH: the settle write is guarded by the identity of the in-flight slot — a
 * load writes only while it is STILL the load registered for its key. `clear()`
 * and `invalidate()` both drop the slot, so a load they superseded is refused on
 * arrival and a later caller never shares its promise. Why: a fetch started
 * before a logout/project-switch clear must not repopulate the cache after it,
 * or the logout hole reopens on timing alone; the list caches make the same
 * trade via `getLogoutEpoch` (logout-handlers). One mechanism covers both
 * lifecycle events and stays per-key: a counter bumped by `invalidate` would
 * refuse every OTHER key's in-flight settle too, turning a single-entry
 * eviction into a cache-wide stall on every WS mutation.
 *
 * INVARIANT: a rejected load is never written as a value, and the in-flight
 * slot is freed on settle AND on rejection, so a retry re-fetches. Why: a cached
 * failure is sticky for the tab session, and a failed value resolved as empty
 * is indistinguishable from "no content" at the render site
 * (no-silent-degradation: error state must not pose as empty state).
 *
 * INVARIANT: bounded to `maxEntries` (LRU) so a long session across many
 * documents cannot grow the map unbounded. Why: no TTL/persistence — eviction
 * is the only bound.
 */

/**
 * The resource-cache primitive: LRU + in-flight dedup (fetch / revalidate) +
 * seed + invalidate + slot-guarded clear, keyed by the caller's id/scope
 * key.
 */
export interface ResourceCache<T> {
  /** Cached value for the key, or undefined. Reading bumps LRU recency. */
  get(key: string): T | undefined;
  /** Store a value WITHOUT a fetch (transclusion seed, SWR revalidated write). Bumps recency, evicts least-recent. */
  seed(key: string, value: T): void;
  /**
   * Cache-then-network fetch with in-flight dedup: a cache hit resolves the
   * cached value without calling `load`; concurrent same-key callers share ONE
   * promise; the settle write is dropped when `clear()` ran mid-flight. The
   * returned promise REJECTS when `load` rejects (failures are never cached).
   */
  fetch(key: string, load: () => Promise<T>): Promise<T>;
  /**
   * Always-revalidate fetch with in-flight dedup: the load ALWAYS runs (a
   * cached value never short-circuits it — read it via `get` for the PRE-gate
   * paint); concurrent same-key callers share ONE promise; the settled value is
   * written unless `clear()` ran mid-flight. The SWR list contract.
   */
  revalidate(key: string, load: () => Promise<T>): Promise<T>;
  /**
   * Drop a single key (a handler mutated data in a way the cache can't mirror)
   * AND the load in flight for it, so a load started before the mutation cannot
   * re-seat the pre-mutation value behind it.
   */
  invalidate(key: string): void;
  /** Drop everything and free the in-flight slots, so mid-flight settles write nothing. */
  clear(): void;
}

export function createResourceCache<T>(maxEntries = 50): ResourceCache<T> {
  // Map preserves insertion order; delete+set moves a key to the newest
  // position, so the first key is always the least-recently-used.
  const map = new Map<string, T>();
  const inflight = new Map<string, Promise<T>>();

  const write = (key: string, value: T): void => {
    map.delete(key);
    map.set(key, value);
    while (map.size > maxEntries) {
      const oldest = map.keys().next().value;
      if (oldest === undefined) break;
      map.delete(oldest);
    }
  };

  const get = (key: string): T | undefined => {
    const v = map.get(key);
    if (v !== undefined) {
      map.delete(key);
      map.set(key, v);
    }
    return v;
  };

  // Shared settle path of `fetch` and `revalidate`: join the one in-flight load
  // for the key (or start it), write the value on settle, free the slot.
  const runLoad = (key: string, load: () => Promise<T>): Promise<T> => {
    const existing = inflight.get(key);
    if (existing) return existing;
    const run: Promise<T> = load().then((value) => {
      // INVARIANT(security): a load writes only while its promise is still the one registered for the key.
      // Why: `clear()` and `invalidate()` drop the slot, so a fetch started before a logout cannot
      // repopulate the cache after it — otherwise the cross-user leak reopens on timing alone — and a
      // WS reference_update landing mid-fetch is not undone by the pre-edit body arriving a moment
      // later and being served afterwards as current. The caller still receives its value; only the
      // cache write is dropped.
      if (inflight.get(key) === run) write(key, value);
      return value;
    });
    inflight.set(key, run);
    // Free the in-flight slot once settled. .then(onFulfilled, onRejected) —
    // NOT .finally — so the side branch resolves even when `run` rejects (a
    // void .finally() would propagate the rejection as an unhandled promise
    // rejection; loads legitimately reject on network/403 so the caller can
    // surface the error).
    const evict = () => { if (inflight.get(key) === run) inflight.delete(key); };
    void run.then(evict, evict);
    return run;
  };

  const cache: ResourceCache<T> = {
    get,
    seed: write,
    fetch(key, load) {
      const cached = get(key);
      if (cached !== undefined) return Promise.resolve(cached);
      return runLoad(key, load);
    },
    revalidate: (key, load) => runLoad(key, load),
    invalidate(key) {
      map.delete(key);
      // Drop the in-flight slot too, for the same reason clear() does: the load
      // it holds was started before the mutation this invalidate reports, so it
      // must neither write nor be shared with the next caller.
      inflight.delete(key);
    },
    clear() {
      map.clear();
      // Drop the in-flight slots too: a post-clear caller must start a fresh
      // load, never share a pre-clear promise. The old run's settle write is
      // refused by the slot-identity check and its evict is a no-op — the slot
      // it remembers is gone.
      inflight.clear();
    },
  };
  return cache;
}
