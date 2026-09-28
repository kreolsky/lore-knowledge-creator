/** SYSTEM: ui-primitives — one-gesture-one-page wheel/trackpad swipe pager.
 *
 * Attaches a non-passive `wheel` listener to a container and maps a single
 * trackpad/wheel GESTURE to exactly one page flip: swipe up (deltaY > 0, natural
 * scroll) → onNext (deeper/older), swipe down (deltaY < 0) → onPrev (newer).
 * Used by HistoryPanel — general, not history-specific.
 *
 * ARCH: gesture-lock model. The listener is
 * bound ONCE (effect deps `[targetRef, enabled]`) and the handlers are read from a
 * ref updated every render — re-binding mid-gesture would reset `locked` and
 * double-flip. `locked` + the quiet timer live in the effect closure (stable across
 * renders), so the lock survives React re-renders triggered by the page flip itself.
 */
import { useEffect, useRef, type RefObject } from 'react';

/** Minimum |deltaY| to START a gesture; filters micro-jitter. Kept small: a swipe is
 *  meant to flip from the first real motion, not wait for a fast flick. */
export const TRIGGER_DELTA = 8;

/** Silence window that ENDS a gesture and unlocks. Long enough to swallow the
 *  trackpad-inertia tail of a single swipe, short enough that two deliberate swipes
 *  (~300 ms apart) each count as their own page flip. */
export const QUIET_MS = 280;

export function useSwipePager(
  targetRef: RefObject<HTMLElement | null>,
  handlers: { onPrev: () => void; onNext: () => void },
  enabled: boolean,
): void {
  // Handlers are read from a ref so the listener never has to be re-bound when they
  // change — re-binding mid-gesture would reset the lock.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    if (!enabled) return;
    const el = targetRef.current;
    if (!el) return;

    let locked = false;
    let quietTimer: ReturnType<typeof setTimeout> | null = null;

    const onWheel = (e: WheelEvent) => {
      // Containers are overflow-hidden; never let native scroll/bounce run.
      e.preventDefault();
      // Ignore predominantly-horizontal pans (horizontal is not paging).
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      const significant = Math.abs(e.deltaY) >= TRIGGER_DELTA;
      // Re-arm the quiet timer ONLY on significant motion. A trackpad gesture's
      // momentum tail emits continuous sub-threshold wheel events; re-arming on
      // those would keep the lock alive through the WHOLE tail, so a fresh swipe
      // couldn't fire until inertia fully died — felt as "must move the cursor
      // before the next swipe works." Ignoring the tail lets the lock release
      // QUIET_MS after the gesture's real motion ends. One gesture still flips
      // exactly one page: the main (significant) burst re-arms + locks.
      if (significant) {
        if (quietTimer) clearTimeout(quietTimer);
        quietTimer = setTimeout(() => { locked = false; }, QUIET_MS);
      }
      // Rest of the gesture is swallowed — one gesture flips exactly one page.
      if (locked) return;
      if (significant) {
        locked = true;
        if (e.deltaY > 0) handlersRef.current.onNext();
        else handlersRef.current.onPrev();
      }
    };

    el.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      el.removeEventListener('wheel', onWheel);
      if (quietTimer) clearTimeout(quietTimer);
    };
  }, [targetRef, enabled]);
}
