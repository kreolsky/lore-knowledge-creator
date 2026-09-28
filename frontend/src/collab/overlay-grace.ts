/**
 * Debounce controller for the blocking collab overlay.
 *
 * INVARIANT: mid-session reconnects under <graceMs> must not raise the blocking overlay.
 * Why: transient blips recover on the first retry; flashing a full-screen blocker
 * interrupts typing. Initial connect (never-been-ready) and a real 'offline' propagate
 * immediately — there is no prior good state to protect, and offline is a real problem
 * the user must see.
 */

export const OVERLAY_GRACE_MS = 1500;

export interface OverlayGraceController {
  /**
   * Feed the current connection readiness.
   * @param ready  connected AND live for the current entity (editor truly usable)
   * @param offline collabStatus === 'offline' (terminal — show immediately)
   */
  update(ready: boolean, offline: boolean): void;
  dispose(): void;
}

/**
 * @param onHiddenChange called with the next `hidden` value (true = overlay suppressed).
 *        Only called on a real transition to avoid redundant React state writes.
 */
export function createOverlayGrace(
  onHiddenChange: (hidden: boolean) => void,
  graceMs: number = OVERLAY_GRACE_MS,
): OverlayGraceController {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let hasBeenReady = false;
  let hidden = true; // start hidden; the initial-connecting branch reveals it at once

  const clear = () => {
    if (timer !== null) { clearTimeout(timer); timer = null; }
  };
  const set = (next: boolean) => {
    if (next !== hidden) { hidden = next; onHiddenChange(hidden); }
  };

  return {
    update(ready: boolean, offline: boolean): void {
      if (ready) {
        clear();
        hasBeenReady = true;
        set(true);
        return;
      }
      if (offline) {
        clear();
        set(false);
        return;
      }
      // Not ready, not offline → connecting/reconnecting (or a mid-switch join window).
      if (!hasBeenReady) {
        // Initial connect: nothing to protect — show immediately.
        clear();
        set(false);
        return;
      }
      // Mid-session drop: delay revealing the blocker by the grace window. If `ready`
      // returns before the timer fires, update(ready=true) cancels it and the overlay
      // never appears.
      if (timer === null) {
        timer = setTimeout(() => { timer = null; set(false); }, graceMs);
      }
    },
    dispose(): void {
      clear();
    },
  };
}
