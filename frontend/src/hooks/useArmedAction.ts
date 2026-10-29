/**
 * // ARCH: Armed-action hook — universal double-click-to-confirm pattern.
 * First click arms (visual feedback via `filled` prop on IconButton),
 * second click executes. Auto-disarms on timeout and mouse leave.
 * Used by: MessageBubble (chat branch delete), RefCard (reference delete),
 * SnapshotActions (checkpoint delete).
 */
// SYSTEM: armed-action — double-click-to-confirm pattern for inline destructive actions

import { useRef, useState, useEffect, useCallback } from 'react';

export interface ArmedActionCore<K> {
  /** The armed key, or null when idle — readable synchronously (see ARCH below). */
  readonly armed: K | null;
  /** Arm `key`. Arming one key disarms the other — two armed keys never coexist. */
  arm(key: K): void;
  disarm(): void;
  /** Armed on `key` → run `fn` and disarm; otherwise arm `key`. */
  handleClick(key: K, fn: () => void): void;
  /** Clear the auto-disarm timer (the holder is going away). */
  dispose(): void;
}

/**
 * The non-React core of the double-click-to-confirm pattern: synchronous armed
 * state for one key at a time plus ONE auto-disarm timer, reported through
 * `onChange` (the React hook mirrors it into useState; the table-block widget
 * re-applies the armed button class). The hook below wraps it with a single
 * key; the table toolbar holds one keyed `'row' | 'col'` (see SYSTEM: armed-action).
 */
// ARCH: handleClick reads the core's synchronous `armed` — avoids stale closure on rapid
//       double-clicks. useState capture at render time; on clicks faster than ~16ms (React
//       frame) the second click sees the old false and re-arms instead of executing fn().
//       The core's `armed` is mutated synchronously on arm → always readable by the next click.
export function createArmedAction<K>(
  timeout: number,
  onChange: (armed: K | null) => void,
): ArmedActionCore<K> {
  let armed: K | null = null;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const set = (key: K | null): void => {
    armed = key;
    if (timer !== undefined) { clearTimeout(timer); timer = undefined; }
    if (key !== null) timer = setTimeout(() => set(null), timeout);
    onChange(key);
  };
  return {
    get armed() { return armed; },
    arm: (key) => set(key),
    disarm: () => { if (armed !== null) set(null); },
    handleClick: (key, fn) => {
      if (armed === key) { set(null); fn(); }
      else set(key);
    },
    dispose: () => { if (timer !== undefined) clearTimeout(timer); timer = undefined; },
  };
}

interface ArmedAction {
  armed: boolean;
  arm: () => void;
  disarm: () => void;
  /** First call arms; second call executes `fn` and disarms. */
  handleClick: (fn: () => void) => void;
}

/** The hook arms ONE button — its single core key. */
type SoleKey = 'on';

export function useArmedAction(timeout = 2000): ArmedAction {
  const [armed, setArmed] = useState(false);
  const coreRef = useRef<ArmedActionCore<SoleKey> | null>(null);
  if (coreRef.current === null) {
    coreRef.current = createArmedAction<SoleKey>(timeout, (a) => setArmed(a !== null));
  }
  const core = coreRef.current;
  useEffect(() => () => core.dispose(), [core]);
  const arm = useCallback(() => core.arm('on'), [core]);
  const disarm = useCallback(() => core.disarm(), [core]);
  const handleClick = useCallback((fn: () => void) => core.handleClick('on', fn), [core]);
  return { armed, arm, disarm, handleClick };
}
