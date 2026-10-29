/** useOutsideDrag — drag one lone element (a chat-generated image thumb) onto
 * the document editor. The gesture half only: a press moved past
 * DRAG_THRESHOLD builds one createRefDropController per drag (ghost, cursor,
 * drop caret, inline embed — the same editor half as the reference-card
 * drag); pointerup commits, pointercancel / Escape / unmount hide. A press
 * released under the threshold stays a click; the click that follows a real
 * drag is swallowed so the element's own click action does not fire. */

import { useEffect, type RefObject } from 'react';
import { DRAG_THRESHOLD, type DragOutsideTarget } from './useSiblingDragReorder';
import { createRefDropController } from '../components/references/refDragAdapter';

export function useOutsideDrag(elementRef: RefObject<HTMLElement | null>, id: string, enabled: boolean): void {
  useEffect(() => {
    const el = elementRef.current;
    if (!el || !enabled) return;
    let press: { x: number; y: number } | null = null;
    let ctl: DragOutsideTarget | null = null;
    const swallowClick = (e: MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      el.removeEventListener('click', swallowClick, true);
    };
    const end = () => {
      press = null;
      ctl = null;
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
      document.removeEventListener('pointercancel', cancel);
    };
    const cancel = () => {
      ctl?.hide();
      end();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && ctl) {
        e.preventDefault();
        cancel();
      }
    };
    const onDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      el.removeEventListener('click', swallowClick, true);
      press = { x: e.clientX, y: e.clientY };
      // WHY document listeners, not pointer capture: the release lands over
      // the editor, and a capture that fails or is lost would deliver it
      // there — the ghost and caret then hang. Same as the card drag.
      document.addEventListener('pointermove', onMove);
      document.addEventListener('pointerup', onUp);
      document.addEventListener('pointercancel', cancel);
    };
    const onMove = (e: PointerEvent) => {
      if (!press) return;
      if (!ctl) {
        if (Math.hypot(e.clientX - press.x, e.clientY - press.y) < DRAG_THRESHOLD) return;
        ctl = createRefDropController(id, 'image');
        el.addEventListener('click', swallowClick, true);
        document.addEventListener('keydown', onKey, true);
      }
      ctl.move(e);
    };
    const onUp = () => {
      if (ctl) ctl.commit(id);
      end();
    };
    el.addEventListener('pointerdown', onDown);
    return () => {
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('click', swallowClick, true);
      cancel();
    };
  }, [elementRef, id, enabled]);
}
