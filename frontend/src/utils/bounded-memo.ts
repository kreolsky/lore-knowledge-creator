/**
 * Bounded LRU memo for content-keyed RENDER results (math html, mermaid svg,
 * measured table heights) — the render-memo counterpart of the resource-cache
 * primitive (api/swr-cache.ts). That one owns network lifecycles (in-flight
 * dedup, seed, generation-guarded clear); this one is a pure result map whose
 * only policy is WHICH entry leaves when the bound is hit: the
 * least-recently-used one, one at a time.
 *
 * WHY LRU, not clear-at-threshold (measured 2026-09-09 on gray with the real
 * katex.renderToString, plan resource-cache-one-primitive step 6): for a single
 * 250-formula document the two policies are indistinguishable — MAX_CACHE is
 * 500, so no eviction pressure exists at all. Under an editing session that
 * CROSSES the bound, clear-at-threshold wipes the whole map mid-edit and the
 * next decoration-rebuild batch recomputed the entire visible window in one
 * frame (worst 40-access batch: 27 katex renders) while LRU never lost the hot
 * set (worst batch: 2 — the genuinely new variants); total recomputes 605 vs
 * 580, wall time within noise. Evicting one cold entry keeps rendering
 * continuous past the threshold; wiping does not.
 *
 * WHY reads bump recency: the hot set in a render memo is the set the layout
 * keeps RE-READING (visible decorations re-render per keystroke; table heights
 * re-read per scroll). An insertion-order bound (the old table-height FIFO)
 * evicts a hot entry that merely stopped being NEW; recency keeps it.
 */

export interface BoundedMemo<T> {
  /** Memo hit for the key, or undefined. Reading bumps recency. */
  get(key: string): T | undefined;
  /** Store a computed result. Refreshes recency; evicts the least-recently-used entry past the bound. */
  set(key: string, value: T): void;
  /** Drop everything (e.g. a theme switch invalidates every rendered color). */
  clear(): void;
  /** Live entry count (bounded by maxEntries). */
  readonly size: number;
}

export function createBoundedMemo<T>(maxEntries: number): BoundedMemo<T> {
  // Map preserves insertion order; delete+set moves a key to the newest
  // position, so the first key is always the least-recently-used (same
  // mechanics as the resource-cache primitive's map).
  const map = new Map<string, T>();
  return {
    get(key: string): T | undefined {
      const v = map.get(key);
      if (v !== undefined) {
        map.delete(key);
        map.set(key, v);
      }
      return v;
    },
    set(key: string, value: T): void {
      map.delete(key);
      map.set(key, value);
      while (map.size > maxEntries) {
        const oldest = map.keys().next().value;
        if (oldest === undefined) break;
        map.delete(oldest);
      }
    },
    clear(): void {
      map.clear();
    },
    get size(): number {
      return map.size;
    },
  };
}
