/** Arrow-key navigation through image references.
 *
 * When an image reference is the active item AND focus is not inside an editing /
 * keyboard-navigable zone, ArrowLeft / ArrowRight cycle through the image references
 * (skipping markdown/audio) in panel order, wrapping around. Mounted only on the
 * primary editor to mirror the useEditorHotkeys invariant.
 *
 * ARCH: window-level keydown, no reactive deps — all store reads happen via
 * useAppStore.getState() inside the handler, identical to useEditorHotkeys.
 *
 * The hook also remembers which way the user last browsed (arrows, or an
 * image→image click in the gallery) so deleting the open image continues in that
 * direction (imageAfterDelete).
 */

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import type { EditorRole } from '../editor/active-editor';
import type { Reference } from '../types';

// WHY module-level: read by the delete path (useReferenceFileDelete), which has
// no handle on this hook; one primary editor per page owns the image viewer.
// Default before any browsing: backward (index shrinking) — user ruling.
let lastBrowseDir: -1 | 1 = -1;

export function resetImageBrowseDirection(): void {
  lastBrowseDir = -1;
}

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

/**
 * The image to open after `deletedId` is deleted from the viewer: its neighbour
 * round the circle in the direction the user last browsed (index growing or
 * shrinking), stepping exactly as that arrow would. Null when it was the only
 * image (or is not an image) — the viewer then returns to the document.
 * `refs` is the list BEFORE removal.
 */
export function imageAfterDelete(refs: Reference[], deletedId: string): Reference | null {
  const imageRefs = refs.filter((r) => r.media_type === 'image');
  const idx = imageRefs.findIndex((r) => r.reference_id === deletedId);
  const next = idx === -1 ? -1 : nextImageIndex(idx, imageRefs.length, lastBrowseDir);
  return next === -1 ? null : imageRefs[next];
}

export function useImageReferenceNav(role: EditorRole): void {
  useEffect(() => {
    if (role === 'secondary') return;
    // Image→image switches from any source (gallery click, arrows) set the
    // direction by index order; the arrow handler then overwrites it with the
    // key's direction, which is right across the wrap-around too.
    const unsubscribe = useAppStore.subscribe((state, prev) => {
      const from = prev.currentReference;
      const to = state.currentReference;
      if (from?.media_type !== 'image' || to?.media_type !== 'image' || from.reference_id === to.reference_id) return;
      const imageIds = state.references.filter((r) => r.media_type === 'image').map((r) => r.reference_id);
      const fromIdx = imageIds.indexOf(from.reference_id);
      const toIdx = imageIds.indexOf(to.reference_id);
      if (fromIdx === -1 || toIdx === -1) return;
      lastBrowseDir = toIdx > fromIdx ? 1 : -1;
    });
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
      lastBrowseDir = dir;
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      unsubscribe();
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [role]);
}
