/**
 * SYSTEM: logout-handlers — dependency-inverted cleanup registry for user-scoped
 * SWR caches (references, AI-chat sessions, note sessions) + per-user store resets.
 *
 * ARCH: app-store's setCurrentUser(null) is the single soft-logout chokepoint (a
 * genuine 401 does a full page reload via apiClient and is already cache-safe). But
 * app-store can't import the chat stores back — they import app-store — so a direct
 * clear would create a cycle. Mirroring the registerAppBridge bridge pattern
 * (ui-store), each cache/store owner registers its reset/clear() here at module load,
 * and setCurrentUser(null) fires them all via clearUserScopedCaches().
 *
 * INVARIANT: a handler MUST be idempotent and never throw. A throw is isolated so one
 * failing clear can't skip the rest. Why: logout must always drop the previous user's
 * SWR cache, otherwise a same-tab re-login into a shared project would paint the prior
 * user's session list (titles/previews) for ~360ms before revalidate — a cross-user
 * info leak.
 *
 * TOCTOU: clearUserScopedCaches also bumps `logoutEpoch`. An
 * in-flight list fetch (loadSessions) started before logout captures the epoch at start
 * and skips its cache+store write if the epoch changed by the time it resolves —
 * otherwise a slow resolve from user A would re-populate the cache / re-commit A's
 * sessions to the store AFTER logout reset undid them. Only the per-user chat/note
 * stores check it (references are project-shared, not a cross-user leak); the counter
 * is centralized here so those store-layer modules can import it without a cycle.
 */

const handlers = new Set<() => void>();
let logoutEpoch = 0;

/** Register a reset/clear to run on soft logout. Idempotent (dedupes by reference). */
export function registerLogoutHandler(fn: () => void): void {
  handlers.add(fn);
}

/** Current logout generation. List fetches capture this at start to detect a mid-flight logout. */
export function getLogoutEpoch(): number {
  return logoutEpoch;
}

/** Run every registered reset/clear. Called by setCurrentUser(null). */
export function clearUserScopedCaches(): void {
  // Bump FIRST: any in-flight list fetch that resolves after this point must see a
  // changed epoch and drop its write (see TOCTOU above).
  logoutEpoch += 1;
  for (const fn of handlers) {
    try {
      fn();
    } catch (e) {
      // WHY: log only — one failing cache clear must not skip the others; logout proceeds.
      console.error('logout handler failed', e);
    }
  }
}
