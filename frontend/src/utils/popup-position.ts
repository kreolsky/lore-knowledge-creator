/** Shared popup positioning: computes top/bottom/maxHeight from anchor rect. */
// SYSTEM: popup-position — reusable vertical positioning for hover/picker popups

import { PREVIEW_WIDTH, PREVIEW_GAP } from './preview-geometry';

const MIN_HEIGHT = 40;

interface PopupPosition {
  top?: number;
  bottom?: number;
  maxH: number;
}

export function computePopupPosition(
  rect: DOMRect,
  desiredMaxH: number,
): PopupPosition {
  const spaceAbove = rect.top - PREVIEW_GAP;
  const spaceBelow = window.innerHeight - rect.bottom - PREVIEW_GAP;
  const below = spaceAbove < spaceBelow || (spaceBelow >= desiredMaxH);

  let top: number | undefined;
  let bottom: number | undefined;
  let maxH: number;

  if (below) {
    maxH = Math.min(desiredMaxH, spaceBelow - 4);
    top = rect.bottom + PREVIEW_GAP;
  } else {
    maxH = Math.min(desiredMaxH, spaceAbove - 4);
    bottom = window.innerHeight - rect.top + PREVIEW_GAP;
  }
  maxH = Math.max(MIN_HEIGHT, maxH);

  return { top, bottom, maxH };
}

export function clampPopupLeft(anchorLeft: number): number {
  return Math.max(4, Math.min(anchorLeft, window.innerWidth - PREVIEW_WIDTH - 12));
}

// Right-panel side-of-panel placement, shared by useRightPanelHoverPreview
// (see SYSTEM: hover-preview — Notes/Refs cards) AND EditorLinkPreview (chat
// links: .chat-source / .chat-link rendered inside <aside class="right-panel">).
// ARCH: ONE formula for both consumers so the two popups cannot drift apart.
/** Vertical nudge subtracted from the computed top/bottom so the popup aligns
 *  near the hovered row rather than flush against its edge. Mirrors the original
 *  useRightPanelHoverPreview shiftOffset. */
export const RIGHT_PANEL_PREVIEW_SHIFT = 60;

/** Left edge for the popup pinned to the side of `.right-panel` (opens over the
 *  editor area to the panel's left, floored at 4). Falls back to the anchor's
 *  left when the panel element is absent (no throw). */
export function getRightPanelPopupLeft(rect: DOMRect): number {
  const panelEl = document.querySelector('.right-panel') as HTMLElement | null;
  const panelLeft = panelEl ? panelEl.getBoundingClientRect().left : rect.left;
  return Math.max(panelLeft - PREVIEW_WIDTH - PREVIEW_GAP, 4);
}

/** Resolve horizontal placement + vertical shift for a hover-preview anchor.
 *  Links inside `.right-panel` (chat sources / chat markdown links) pin to the
 *  side of the panel; editor links keep near-link placement. Pure + testable. */
export function chooseLinkPlacement(el: HTMLElement, rect: DOMRect): {
  left: number;
  topShift: number;
} {
  const inRightPanel = !!el.closest('.right-panel');
  if (inRightPanel) {
    return { left: getRightPanelPopupLeft(rect), topShift: RIGHT_PANEL_PREVIEW_SHIFT };
  }
  return { left: clampPopupLeft(rect.left), topShift: 0 };
}

/** Position a preview popup beside a picker/list box — to the right of the box
 *  when there is room, otherwise flipped to its left. Shared by the three pickers
 *  (LinkSuggestions, ParentPicker, ContentPicker), which previously each carried an
 *  identical copy of this side-decide + clamp block. maxHeight caps at `maxHeightCap`
 *  and at the viewport minus `bottomMargin` (pickers reserve different bottom space).
 *  See SYSTEM: preview-geometry. */
export function computeBesideBoxPreviewPosition(
  boxLeft: number,
  boxTop: number,
  boxWidth: number,
  maxHeightCap: number,
  bottomMargin = 20,
): { top: number; left: number; maxHeight: number } {
  const boxRight = boxLeft + boxWidth;
  const side = (window.innerWidth - boxRight) >= PREVIEW_WIDTH + PREVIEW_GAP ? 'right' : 'left';
  return {
    top: boxTop,
    left: side === 'right' ? boxRight + PREVIEW_GAP : boxLeft - PREVIEW_WIDTH - PREVIEW_GAP,
    maxHeight: Math.min(maxHeightCap, window.innerHeight - boxTop - bottomMargin),
  };
}

