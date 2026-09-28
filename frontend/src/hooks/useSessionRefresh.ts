/**
 * Session-refresh scheduler — keeps the 14-day sliding-window cookie alive for
 * long-lived SPA tabs by re-calling GET /api/auth/me periodically and on tab regain.
 *
 * // SYSTEM: session-refresh — periodic + visibility-driven /me poll to slide cookie expiry
 *
 * WHY a scheduler (not a React hook): the refresh must run regardless of which route
 * is mounted, so it is started once from AuthGuard's mount effect and torn down on
 * unmount. Returning a plain stop() keeps it unit-testable without a React tree.
 *
 * Error policy: a genuine 401 flows through apiClient (which does the
 * hard redirect to '/'). Transient failures (502/503 or network TypeError during deploy)
 * are reported via onTransientFailure so the caller can toast — they MUST NOT logout.
 */
import { apiClient, ServiceUnavailableError, AuthError } from '../api/client';

export const SESSION_REFRESH_INTERVAL_MS = 6 * 60 * 60 * 1000;

export interface SessionRefreshOptions {
  /** Called on transient failure (502/503/network) — caller shows a toast, no redirect. */
  onTransientFailure: (err: unknown) => void;
  /**
   * Skip the immediate bootstrap refresh. Set true when the caller has JUST fetched
   * /auth/me (SessionRoute's session probe, which AuthGuard mounts behind) — the
   * window is already slid, so a bootstrap would only duplicate that request.
   */
  skipInitialRefresh?: boolean;
}

function isTransient(err: unknown): boolean {
  return err instanceof ServiceUnavailableError || err instanceof TypeError;
}

/**
 * Start the session-refresh scheduler. Returns a `stop()` function.
 *
 * // ARCH: returns the stop fn directly (not a handle object) so it composes cleanly
 * // as `const stop = startSessionRefresh(...); useEffect(() => stop, [])`.
 */
export function startSessionRefresh(opts: SessionRefreshOptions): () => void {
  let stopped = false;
  const intervalId = window.setInterval(refresh, SESSION_REFRESH_INTERVAL_MS);

  function onVisibility() {
    if (stopped) return;
    // WHY: only refresh when the tab returns to the foreground — a tab that goes
    // hidden has no user activity to keep the session warm and would just spin
    // a redundant request against the backend.
    if (document.visibilityState === 'visible') refresh();
  }
  document.addEventListener('visibilitychange', onVisibility);

  async function refresh() {
    try {
      await apiClient.get('/auth/me');
    } catch (err) {
      // 401 is a genuine expiry — apiClient already redirected to '/'. Do not toast.
      if (err instanceof AuthError) return;
      if (isTransient(err)) {
        opts.onTransientFailure(err);
        return;
      }
      // Unknown error — surface as transient rather than silently swallowing, per
      // the no-silent-degradation rule.
      opts.onTransientFailure(err);
    }
  }

  // Bootstrap: skipped when the caller just fetched /me (SessionRoute's session
  // probe) — the window is already slid, so an extra /me would duplicate it.
  if (!opts.skipInitialRefresh) refresh();

  function stop() {
    if (stopped) return;
    stopped = true;
    window.clearInterval(intervalId);
    document.removeEventListener('visibilitychange', onVisibility);
  }

  return stop;
}
