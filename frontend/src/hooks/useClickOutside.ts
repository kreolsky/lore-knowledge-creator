/** SYSTEM: ui-primitives — shared outside-click detection hook.
 *
 * Bubbling-phase `mousedown` on `document` + `contains` over one ref or several (a popup
 * plus its preview pane / trigger): outside means outside ALL of them. Capture-phase or
 * editor-coupled dismissals stay hand-written at their sites.
 *
 * ARCH: listener attaches only while `enabled` (default true) and fires only while the first ref is mounted,
 * so consumers can gate it on `open` state. The handler is re-bound when it changes
 * but the listener is stable (removed on cleanup) to avoid duplicate attaches.
 */

import { useEffect, useRef } from 'react';

type OutsideRef = React.RefObject<HTMLElement | null> | undefined;

export type UseClickOutsideFn = (
  refs: OutsideRef | OutsideRef[],
  handler: () => void,
  enabled?: boolean,
) => void;

export function useClickOutside(
  refs: OutsideRef | OutsideRef[],
  handler: () => void,
  enabled = true,
): void {
  // WHY: callers pass a fresh array literal per render; reading it through a ref keeps
  // the listener bound once per (handler, enabled) instead of once per render.
  const refsRef = useRef(refs);
  refsRef.current = refs;
  useEffect(() => {
    if (!enabled) return;
    function onMouseDown(e: MouseEvent) {
      const list = Array.isArray(refsRef.current) ? refsRef.current : [refsRef.current];
      const target = e.target as Node;
      // INVARIANT: fires only while the FIRST ref is mounted, and only for a press outside
      // every mounted ref; an unmounted secondary ref (a preview pane not shown) is skipped.
      // Why: the first ref is the popup itself — no popup, nothing to dismiss; the others
      // are panes that belong to it, so a click in a picker's preview must keep it open.
      if (!list[0]?.current) return;
      if (list.some(r => r?.current?.contains(target))) return;
      handler();
    }
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [handler, enabled]);
}
