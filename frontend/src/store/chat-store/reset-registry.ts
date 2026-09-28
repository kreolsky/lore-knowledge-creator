/**
 * SYSTEM: chat-reset-registry — dependency-inverted cleanup registry for
 * chat-store module-level caches (children-map + active-path memo, in-flight
 * session patches/loads, pending context PATCH timers, auto-title success set).
 *
 * ARCH: mirrors logout-handlers.ts. misc-slice.reset is the chat-
 * reset chokepoint (project switch / logout), but it cannot enumerate every
 * module-level cache that needs clearing — each cache owner registers its clear()
 * here at module load, and reset() fires them all via clearChatCaches(). This
 * removes the "remember to extend reset()" footgun: a future cache that forgets to
 * register simply isn't cleared (no worse than today); one that self-registers is
 * cleared for free.
 *
 * INVARIANT: a handler MUST be idempotent and never throw. A throw is isolated so
 * one failing clear cannot skip the rest (copied from logout-handlers.ts). Why: a
 * reset runs on project switch / logout — a throw there must never leave half the
 * caches stale (a stale children-map, for instance, would orphan root messages on
 * the next view because buildChildrenMap caches by message-array reference).
 *
 * DAG rule: this module imports NOTHING from the cache owners — they import IT
 * (same direction as logout-handlers.ts). Reversing that creates a cycle.
 */

const handlers = new Set<() => void>();

/** Register a cache clear() to run on chat reset. Idempotent (dedupes by reference). */
export function registerChatResetHandler(fn: () => void): void {
  handlers.add(fn);
}

/** Run every registered clear(). Called by misc-slice.reset(). Never throws. */
export function clearChatCaches(): void {
  for (const fn of handlers) {
    try {
      fn();
    } catch (e) {
      // WHY: log only — one failing clear must not skip the others (the contract is "never throws").
      console.error('chat reset handler failed', e);
    }
  }
}

/** Test-only: wipe the registry so unit tests start from a known-empty set. */
export function __resetRegistryForTest(): void {
  handlers.clear();
}
