/**
 * useGearMenu — open state of a row's burger (gear) menu.
 *
 * Opens on 400ms hover OR immediately on click/tap of the burger icon; closes on the
 * consumer's mouseleave (`close`) or on a press outside `ref`. Shared by DocumentTree
 * rows and RowActions so the two burgers cannot drift apart.
 */

import { useState, useRef, useCallback } from 'react';
import type React from 'react';
import { useClickOutside } from './useClickOutside';

const HOVER_OPEN_DELAY_MS = 400;

export function useGearMenu<T extends HTMLElement = HTMLDivElement>() {
  const [open, setOpen] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ref = useRef<T | null>(null);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleEnter = useCallback(() => {
    clearTimer();
    timerRef.current = setTimeout(() => setOpen(true), HOVER_OPEN_DELAY_MS);
  }, [clearTimer]);

  const close = useCallback(() => {
    clearTimer();
    setOpen(false);
  }, [clearTimer]);

  // WHY: touch has no hover — the tap's compat mouseenter only arms the 400ms timer, so
  // the click opens the menu at once (and stops the row underneath from navigating).
  const handleIconClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    clearTimer();
    setOpen(true);
  }, [clearTimer]);

  // WHY: a touch device may never fire mouseleave — a press elsewhere is the close gesture.
  useClickOutside(ref, close, open);

  return { open, ref, handleEnter, close, handleIconClick } as const;
}
