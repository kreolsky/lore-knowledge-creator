/**
 * logout-handlers — dependency-inverted cleanup registry for user-scoped caches.
 *
 * app-store can't import the chat stores back (they import app-store), so each
 * store registers its SWR-cache clear() here; setCurrentUser(null) runs them all.
 * Mirrors the registerAppBridge bridge pattern (ui-store).
 */
import { describe, it, expect, vi } from 'vitest';
import { registerLogoutHandler, clearUserScopedCaches, getLogoutEpoch } from './logout-handlers';

describe('logout-handlers registry', () => {
  it('runs a registered handler on clearUserScopedCaches', () => {
    const fn = vi.fn();
    registerLogoutHandler(fn);
    clearUserScopedCaches();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('isolates a throwing handler so the rest still run', () => {
    const ok = vi.fn();
    // Register a handler that throws, then a healthy one after it. The Set
    // preserves insertion order, so the throwing one runs first.
    registerLogoutHandler(() => { throw new Error('boom'); });
    registerLogoutHandler(ok);
    clearUserScopedCaches();
    expect(ok).toHaveBeenCalledTimes(1);
  });

  it('does not invoke the same handler twice (idempotent registration)', () => {
    const fn = vi.fn();
    registerLogoutHandler(fn);
    registerLogoutHandler(fn);
    clearUserScopedCaches();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('bumps the logout epoch so in-flight list fetches detect a mid-flight logout', () => {
    // TOCTOU: clearUserScopedCaches must advance the epoch each call so a fetch that
    // captured the old generation sees a mismatch and drops its write.
    const before = getLogoutEpoch();
    clearUserScopedCaches();
    expect(getLogoutEpoch()).toBe(before + 1);
    clearUserScopedCaches();
    expect(getLogoutEpoch()).toBe(before + 2);
  });
});
