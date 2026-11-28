/** Lightweight vertical drag-to-resize hook. Returns controlled height + ref for the drag handle. */
// SYSTEM: vertical-resizer — drag handle between chat messages and input area

import { useState, useRef, useCallback, useEffect } from 'react';

interface UseVerticalDragResizeOptions {
  defaultHeight: number;
  minHeight: number;
  maxHeight: number;
  /** localStorage key; when set, the dragged height persists and replaces defaultHeight on load. */
  storageKey?: string;
}

interface UseVerticalDragResizeReturn {
  height: number;
  setHeight: (h: number) => void;
  handleRef: React.RefObject<HTMLDivElement | null>;
}

export function useVerticalDragResize(options: UseVerticalDragResizeOptions): UseVerticalDragResizeReturn {
  const { defaultHeight, minHeight, maxHeight, storageKey } = options;

  const [height, setHeightState] = useState<number>(defaultHeight);
  const heightRef = useRef<number>(defaultHeight);
  const storageKeyRef = useRef(storageKey);
  storageKeyRef.current = storageKey;

  // WHY: re-resolve when the key/default changes (the composer switches modes without remounting).
  // A stored value means the user dragged it; otherwise the default applies.
  useEffect(() => {
    let stored: number | null = null;
    try {
      const raw = storageKey ? localStorage.getItem(storageKey) : null;
      stored = raw ? Number(raw) : null;
    } catch { /* storage unavailable: the default applies */ }
    const initial = stored && Number.isFinite(stored) ? stored : defaultHeight;
    const clamped = Math.min(maxHeight, Math.max(minHeight, initial));
    heightRef.current = clamped;
    setHeightState(clamped);
  }, [storageKey, defaultHeight, minHeight, maxHeight]);
  const handleRef = useRef<HTMLDivElement | null>(null);
  const dragging = useRef(false);
  const startY = useRef(0);
  const startH = useRef(0);

  const setHeight = useCallback((h: number) => {
    const clamped = Math.min(maxHeight, Math.max(minHeight, h));
    heightRef.current = clamped;
    setHeightState(clamped);
  }, [minHeight, maxHeight]);

  useEffect(() => {
    // WHY: Pointer Events (not mouse*) so the drag works for touch (tablet) as well as
    // mouse/pen. Touch screens never emit mouse* — see useResizer.ts.
    const handlePointerDown = (e: PointerEvent) => {
      if (!handleRef.current || !handleRef.current.contains(e.target as Node)) return;
      e.preventDefault();
      dragging.current = true;
      startY.current = e.clientY;
      startH.current = heightRef.current;
      handleRef.current.classList.add('dragging');
      document.body.classList.add('is-resizing');
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
    };

    const handlePointerMove = (e: PointerEvent) => {
      if (!dragging.current) return;
      const delta = startY.current - e.clientY;
      const newH = startH.current + delta;
      const clamped = Math.min(maxHeight, Math.max(minHeight, newH));
      heightRef.current = clamped;
      setHeightState(clamped);
    };

    const handlePointerUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      if (handleRef.current) handleRef.current.classList.remove('dragging');
      document.body.classList.remove('is-resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      const key = storageKeyRef.current;
      if (!key) return;
      try { localStorage.setItem(key, String(heightRef.current)); } catch { /* not persisted: per-viewer convenience */ }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('pointermove', handlePointerMove);
    document.addEventListener('pointerup', handlePointerUp);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('pointermove', handlePointerMove);
      document.removeEventListener('pointerup', handlePointerUp);
    };
  }, [minHeight, maxHeight]);

  return { height, setHeight, handleRef };
}
