/**
 * Shared hover preview logic: 300ms delay, popup positioning, and mouse-transition detection.
 *
 * Extracted from DocumentTree and ReferencesPanel which had identical implementations.
 * Consumers provide a custom `getPopupLeft` to position the popup relative to their
 * own layout context (sidebar, right panel, etc). Entity ID tracking is done by the
 * consumer via a separate state variable set before calling handleHover.
 */
// SYSTEM: hover-preview — shared delay+position hook for hover preview popups
// ARCH: Extracted from DocumentTree and ReferencesPanel which had identical delay+position+transition
// logic. Entity ID tracked separately by consumer via local state.

import { useState, useRef, useCallback } from 'react';
import { computePopupPosition } from '../utils/popup-position';
import { PREVIEW_MAX_HEIGHT } from '../utils/preview-geometry';
import { usePopupSlot } from './usePopupSlot';

interface HoverPreviewState {
  top?: number;
  bottom?: number;
  left: number;
  maxHeight: number;
}

interface UseHoverPreviewOptions {
  delay?: number;
  maxHeight?: number;
  shiftOffset?: number;
  getPopupLeft: (anchorRect: DOMRect) => number;
}

export function useHoverPreview(options: UseHoverPreviewOptions) {
  const { delay = 300, maxHeight = PREVIEW_MAX_HEIGHT, shiftOffset = 0, getPopupLeft } = options;

  const [state, setState] = useState<HoverPreviewState | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const popupRef = useRef<HTMLDivElement | null>(null);
  const popupRootCallback = useCallback((el: HTMLDivElement | null) => {
    popupRef.current = el;
  }, []);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const handleHover = useCallback((el: HTMLElement) => {
    clearTimer();
    timerRef.current = setTimeout(() => {
      const rect = el.getBoundingClientRect();
      const pos = computePopupPosition(rect, maxHeight);
      setState({
        top: pos.top != null ? pos.top - shiftOffset : undefined,
        bottom: pos.bottom != null ? pos.bottom - shiftOffset : undefined,
        left: getPopupLeft(rect),
        maxHeight: pos.maxH,
      });
    }, delay);
  }, [clearTimer, delay, maxHeight, shiftOffset, getPopupLeft]);

  const handleHoverLeave = useCallback((e?: React.MouseEvent) => {
    clearTimer();
    if (e && popupRef.current && popupRef.current.contains(e.relatedTarget as Node | null)) return;
    setState(null);
  }, [clearTimer]);

  // INVARIANT: gate visibility on owning the shared popup slot. Why: opening a higher-priority
  // popup ("change parent" etc.) must hide the hover preview — only one floating popup at a time.
  // Gating alone (not clearing state) avoids wiping the preview during the one-render acquire lag.
  const isActive = usePopupSlot('hover-preview', state !== null);

  return {
    visible: state !== null && isActive,
    state,
    handleHover,
    handleHoverLeave,
    popupRootCallback,
  } as const;
}
