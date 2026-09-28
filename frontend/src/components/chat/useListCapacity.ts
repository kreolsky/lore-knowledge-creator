/** useListCapacity — measures a scroll container + its first row and returns how many
 *  uniform-height rows fit (an adaptive page size). Falls back to a constant before
 *  the first measurement and when ResizeObserver is unavailable. // SYSTEM: pill-list
 *
 *  No magic pixel constants: paddings are read via getComputedStyle and the row height
 *  via offsetHeight, so the calc can't drift from index.css / ListPill.module.css. */
import { useState, useRef, useLayoutEffect, type RefObject } from 'react';
import { computeCapacity } from './capacity';

/**
 * @param containerRef  the scroll container (PillList) — its clientHeight is measured.
 * @param firstRowRef   optional explicit row to measure; when null, the container's
 *                      firstElementChild is used (ListPill has no ref passthrough).
 * @param itemCount     total items available to paginate over.
 * @param fallback      capacity used before the first measurement / when RO is absent.
 */
export function useListCapacity(
  containerRef: RefObject<HTMLElement | null>,
  firstRowRef: RefObject<HTMLElement | null> | null,
  itemCount: number,
  fallback: number,
): number {
  const initial = Math.min(Math.max(itemCount, 0), fallback);
  const [capacity, setCapacity] = useState(initial);
  // Tracks the last capacity we dispatched so we skip setCapacity entirely when the
  // integer value is unchanged — a deterministic guard against resize render storms.
  const lastCapacity = useRef(initial);

  useLayoutEffect(() => {
    const measure = () => {
      const container = containerRef.current;
      if (!container) return;
      const row = (firstRowRef?.current ?? container.firstElementChild) as HTMLElement | null;
      if (!row) return;
      const cs = getComputedStyle(container);
      const padTop = parseFloat(cs.paddingTop) || 0;
      const padBottom = parseFloat(cs.paddingBottom) || 0;
      const next = computeCapacity(container.clientHeight, padTop, padBottom, row.offsetHeight, itemCount);
      if (lastCapacity.current !== next) {
        lastCapacity.current = next;
        setCapacity(next);
      }
    };

    measure();

    // Guard for SSR / very old browsers lacking ResizeObserver (safety net only).
    const Ctor = typeof ResizeObserver === 'undefined' ? null : ResizeObserver;
    if (!Ctor) return;

    const ro = new Ctor(measure);
    const container = containerRef.current;
    if (container) ro.observe(container);
    const row = (firstRowRef?.current ?? container?.firstElementChild) as HTMLElement | null;
    if (row) ro.observe(row);
    return () => ro.disconnect();
  }, [itemCount, containerRef, firstRowRef]);

  return capacity;
}
