/** Arrow-key navigation through image references.
 *
 * When an image reference is the active item AND focus is not inside an editing /
 * keyboard-navigable zone, ArrowLeft / ArrowRight cycle through the image references
 * (skipping markdown/audio) in panel order, wrapping around. Mounted only on the
 * primary editor to mirror the useEditorHotkeys invariant.
 *
 * ARCH: window-level keydown, no reactive deps — all store reads happen via
 * useAppStore.getState() inside the handler, identical to useEditorHotkeys.
 */

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import type { EditorRole } from '../editor/active-editor';

/**
 * Pure index/wrap math. Returns -1 if there is nothing to cycle.
 */
export function nextImageIndex(currentIdx: number, count: number, dir: -1 | 1): number {
  if (count <= 1) return -1;
  return (currentIdx + dir + count) % count;
}

/**
 * True when focus is in a zone that owns arrow keys (editor text, inputs,
 * contenteditable, or the document tree). Pure + testable.
 */
export function isFocusInEditableZone(activeEl: Element | null): boolean {
  if (!activeEl) return false;
  if (activeEl.closest('.cm-content')) return true;
  const tag = activeEl.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true;
  const ce = (activeEl as HTMLElement).isContentEditable;
  if (ce || activeEl.getAttribute('contenteditable') === 'true') return true;
  if (activeEl.closest('[role="tree"]')) return true;
  return false;
}

export function useImageReferenceNav(role: EditorRole): void {
  useEffect(() => {
    if (role === 'secondary') return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const state = useAppStore.getState();
      if (state.currentReference?.media_type !== 'image') return;
      if (isFocusInEditableZone(document.activeElement)) return;

      const imageRefs = state.references.filter((r) => r.media_type === 'image');
      if (imageRefs.length < 2) return;

      const currentIdx = imageRefs.findIndex(
        (r) => r.reference_id === state.currentReference!.reference_id,
      );
      if (currentIdx === -1) return;

      const dir: -1 | 1 = e.key === 'ArrowRight' ? 1 : -1;
      const next = nextImageIndex(currentIdx, imageRefs.length, dir);
      e.preventDefault();
      state.setCurrentReference(imageRefs[next]);
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [role]);
}
