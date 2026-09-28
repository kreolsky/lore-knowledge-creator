/** SYSTEM: ui-primitives — shared outside-click detection hook.
 *
 * Encapsulates the `document.addEventListener('mousedown', …)` + `ref.current.contains`
 * pattern that was hand-rolled across the composer selectors, ChatHeader dropdowns,
 * and DownloadMenu. Same behavior as the inline effects it replaces (mousedown phase +
 * contains check).
 *
 * ARCH: listener attaches only while `enabled` (default true) and a ref is present,
 * so consumers can gate it on `open` state. The handler is re-bound when it changes
 * but the listener is stable (removed on cleanup) to avoid duplicate attaches.
 */

import { useEffect } from 'react';

export type UseClickOutsideFn = (
  ref: React.RefObject<HTMLElement | null>,
  handler: () => void,
  enabled?: boolean,
) => void;

export function useClickOutside(
  ref: React.RefObject<HTMLElement | null>,
  handler: () => void,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled) return;
    function onMouseDown(e: MouseEvent) {
      const el = ref.current;
      if (el && !el.contains(e.target as Node)) handler();
    }
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [ref, handler, enabled]);
}
