/** Drag-to-resize hook for sidebar and right panel with snap-to-close. */
// ARCH: Mixed controlled/uncontrolled hook.
// - isOpen: controlled by parent (store is single source of truth). No internal state duplication.
// - width: uncontrolled (one-time seed via initialWidth). Hook owns width during drag for
//   performance; parent is notified on mouseUp via onWidthChange callback.
// SYSTEM: panel-resizer — drag-to-resize hook with snap-to-close for sidebar/panels

import type React from 'react';
import { useState, useRef, useCallback, useEffect } from 'react';

interface UseResizerOptions {
  side: 'left' | 'right';
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  snapCloseThreshold?: number;
  initialWidth?: number;
  isOpen?: boolean;
  onWidthChange?: (width: number) => void;
  onOpenChange?: (open: boolean) => void;
}

interface UseResizerReturn {
  width: number;
  isOpen: boolean;
  resizerRef: React.RefObject<HTMLDivElement | null>;
  open: (w?: number) => void;
  close: () => void;
  setWidth: (w: number) => void;
}

const clamp = (v: number, min: number, max: number) => Math.min(max, Math.max(min, v));

export function useResizer(options: UseResizerOptions): UseResizerReturn {
  const { side, defaultWidth, minWidth, maxWidth, snapCloseThreshold = 35,
          initialWidth, onWidthChange, onOpenChange } = options;

  const isOpen = options.isOpen ?? true;

  const [width, _setWidth] = useState<number>(clamp(initialWidth ?? defaultWidth, minWidth, maxWidth));

  // widthRef mirrors width state so event handlers never need `width` in their closure
  const widthRef   = useRef<number>(width);
  const savedWidth = useRef<number>(width);
  const resizerRef = useRef<HTMLDivElement | null>(null);
  const dragging   = useRef(false);
  const startX     = useRef(0);
  const startW     = useRef(0);

  // Keep callback refs stable to avoid re-registering listeners
  const onWidthChangeRef = useRef(onWidthChange);
  onWidthChangeRef.current = onWidthChange;
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  const setWidth = useCallback((w: number) => {
    widthRef.current = w;
    _setWidth(w);
  }, []);

  const open = useCallback((w?: number) => {
    const targetWidth = w || savedWidth.current || defaultWidth;
    setWidth(targetWidth);
    onOpenChangeRef.current?.(true);
  }, [defaultWidth, setWidth]);

  const close = useCallback(() => {
    savedWidth.current = widthRef.current;
    onOpenChangeRef.current?.(false);
  }, []);

  useEffect(() => {
    // WHY: Pointer Events (not mouse*) so the same drag works for mouse, touch (tablet),
    // and pen. Touch screens never emit mouse* — mouse-only handlers left tablet panels
    // un-resizable. Listeners are on `document`, so no setPointerCapture is needed.
    const handlePointerDown = (e: PointerEvent) => {
      if (!resizerRef.current || !resizerRef.current.contains(e.target as Node)) return;

      e.preventDefault();
      dragging.current = true;
      startX.current   = e.clientX;
      startW.current   = widthRef.current;
      resizerRef.current.classList.add('dragging');
      document.body.classList.add('is-resizing');
      document.body.style.cursor     = 'col-resize';
      document.body.style.userSelect = 'none';
    };

    const handlePointerMove = (e: PointerEvent) => {
      if (!dragging.current) return;

      const delta = side === 'left'
        ? e.clientX - startX.current
        : startX.current - e.clientX;

      const newW = startW.current + delta;

      if (newW < snapCloseThreshold) {
        dragging.current = false;
        if (resizerRef.current) resizerRef.current.classList.remove('dragging');
        document.body.classList.remove('is-resizing');
        document.body.style.cursor     = '';
        document.body.style.userSelect = '';
        savedWidth.current = startW.current;
        onOpenChangeRef.current?.(false);
        return;
      }

      setWidth(Math.min(maxWidth, Math.max(minWidth, newW)));
    };

    const handlePointerUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      if (resizerRef.current) resizerRef.current.classList.remove('dragging');
      document.body.classList.remove('is-resizing');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      // Notify caller of final width
      onWidthChangeRef.current?.(widthRef.current);
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('pointermove', handlePointerMove);
    document.addEventListener('pointerup',   handlePointerUp);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('pointermove', handlePointerMove);
      document.removeEventListener('pointerup',   handlePointerUp);
    };
    // NOTE: `width` and `isOpen` intentionally omitted — we use refs inside handlers
    // to avoid re-registering listeners on every pixel change
  }, [side, minWidth, maxWidth, snapCloseThreshold, setWidth]);

  return { width, isOpen, resizerRef, open, close, setWidth };
}
