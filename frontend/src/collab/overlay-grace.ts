/**
 * Debounce controller for the blocking collab overlay.
 *
 * INVARIANT: mid-session reconnects under <graceMs> must not raise the blocking overlay.
 * Why: transient blips recover on the first retry; flashing a full-screen blocker
 * interrupts typing. A real 'offline' propagates immediately — offline is a real problem
 * the user must see.
 *
 * INVARIANT: an initial connect under <initialDelayMs> must not raise the overlay either.
 * Why: on a normal link it finishes within a few hundred ms, and the document's text is
 * already on screen, so the overlay only flashes a blur over it. Input stays blocked for
 * that window by the editor's own `editable` gate, not by this overlay.
 */

export const OVERLAY_GRACE_MS = 1500;
export const INITIAL_OVERLAY_DELAY_MS = 300;

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
  initialDelayMs: number = INITIAL_OVERLAY_DELAY_MS,
): OverlayGraceController {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let hasBeenReady = false;
  let hidden = true; // start hidden; a connect that outlasts its delay reveals it

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
      // Delay revealing the blocker. If `ready` returns before the timer fires,
      // update(ready=true) cancels it and the overlay never appears.
      if (timer === null) {
        const delay = hasBeenReady ? graceMs : initialDelayMs;
        timer = setTimeout(() => { timer = null; set(false); }, delay);
      }
    },
    dispose(): void {
      clear();
    },
  };
}
