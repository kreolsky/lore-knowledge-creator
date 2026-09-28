/** computeCapacity — pure height-based page-size math for adaptive list pagination.
 *  ResizeObserver-free: takes already-measured pixel values so it can be unit-tested
 *  without a DOM. Used by useListCapacity (chat/notes empty-list pagination). */

/**
 * How many uniform-height rows fit inside a scroll container's content box.
 *
 * @param containerH  container.clientHeight (includes the bottom reserve padding).
 * @param padTop      getComputedStyle(container).paddingTop (px).
 * @param padBottom   getComputedStyle(container).paddingBottom (px).
 * @param rowHeight   offsetHeight of a single rendered row (px).
 * @param itemCount   total number of items available to paginate over.
 * @returns integer in [1, itemCount], or 0 when itemCount === 0.
 */
export function computeCapacity(
  containerH: number,
  padTop: number,
  padBottom: number,
  rowHeight: number,
  itemCount: number,
): number {
  if (itemCount <= 0) return 0;
  // Guard against divide-by-zero / negative row height (defensive; CSS should
  // always yield a positive height, but never throw on a bad measurement).
  if (rowHeight <= 0) return 1;
  const usable = containerH - padTop - padBottom;
  const fit = Math.floor(usable / rowHeight);
  return Math.min(Math.max(fit, 1), itemCount);
}
