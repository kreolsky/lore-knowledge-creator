/**
 * // ARCH: Armed-action hook — universal double-click-to-confirm pattern.
 * First click arms (visual feedback via `filled` prop on IconButton),
 * second click executes. Auto-disarms on timeout and mouse leave.
 * Used by: MessageBubble (chat branch delete), RefCard (reference delete),
 * SnapshotActions (checkpoint delete).
 */
// SYSTEM: armed-action — double-click-to-confirm pattern for inline destructive actions
// ARCH: handleClick reads armed via useRef — avoids stale closure on rapid double-clicks.
//       useState capture at render time; on clicks faster than ~16ms (React frame) the
//       second click sees the old false and re-arms instead of executing fn(). useRef
//       is mutated synchronously on setArmed → always readable by the next click.

import { useRef, useState, useEffect, useCallback } from 'react';

interface ArmedAction {
  armed: boolean;
  arm: () => void;
  disarm: () => void;
  /** First call arms; second call executes `fn` and disarms. */
  handleClick: (fn: () => void) => void;
}

export function useArmedAction(timeout = 2000): ArmedAction {
  const [armed, setArmed] = useState(false);
  const armedRef = useRef(false);

  useEffect(() => {
    if (!armed) return;
    const timer = setTimeout(() => { setArmed(false); armedRef.current = false; }, timeout);
    return () => clearTimeout(timer);
  }, [armed, timeout]);

  const arm = useCallback(() => { setArmed(true); armedRef.current = true; }, []);
  const disarm = useCallback(() => { setArmed(false); armedRef.current = false; }, []);
  const handleClick = useCallback(
    (fn: () => void) => {
      if (armedRef.current) { fn(); setArmed(false); armedRef.current = false; }
      else { setArmed(true); armedRef.current = true; }
    },
    [],
  );

  return { armed, arm, disarm, handleClick };
}
