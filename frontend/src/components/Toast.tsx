/**
 * Toast notification banner — single-instance, auto-dismiss after 3s.
 *
 * SYSTEM: toast — single-instance auto-dismiss notification banner (error/info).
 *
 * State lives in app-store: `toast` (current message or null), `showToast(msg, type)`,
 * `clearToast()`. Only one toast visible at a time — new toast replaces previous.
 *
 * Types: 'error' (red left border) — default, for failures; 'info' (blue left border) —
 * for confirmations like "copied to clipboard"; 'warning' (amber left border) — for
 * transient issues like reconnecting.
 *
 * Persistent mode: pass `{ persistent: true }` as third arg to `showToast`. The toast
 * stays visible until explicitly cleared — used for connection status messages that
 * resolve on reconnect.
 *
 * Usage from React: `const showToast = useAppStore(s => s.showToast)`
 * Usage from non-React (stores, event handlers): `useAppStore.getState().showToast(msg)`
 */

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';

export function Toast() {
  const toast = useAppStore(s => s.toast);
  const clearToast = useAppStore(s => s.clearToast);

  useEffect(() => {
    if (!toast || toast.persistent) return;
    const timer = setTimeout(clearToast, 3000);
    return () => clearTimeout(timer);
  }, [toast, clearToast]);

  if (!toast) return null;

  return (
    <div role="alert" aria-live="assertive" className={`toast toast--${toast.type}`} onClick={clearToast}>
      {toast.message}
    </div>
  );
}
