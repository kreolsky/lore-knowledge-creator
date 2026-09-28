/**
 * Claims the single floating-popup slot for one popup instance.
 *
 * Returns `isActive`. The caller MUST render its portal only when `isActive === true`.
 * `wantOpen && !isActive` means the popup was suppressed by a higher-priority one — render nothing.
 */
// SYSTEM: popups — React binding for the popup slot store

import { useEffect, useRef } from 'react';
import { usePopupStore } from '../store/popup-store';
import type { PopupId } from '../components/popup-config';

export function usePopupSlot(id: PopupId, wantOpen: boolean): boolean {
  const tokenRef = useRef<number | null>(null);

  // Subscribe to ownership primitives so we re-render when the slot changes hands.
  const activeId = usePopupStore(s => s.activeId);
  const activeToken = usePopupStore(s => s.activeToken);

  useEffect(() => {
    if (!wantOpen) return;
    tokenRef.current = usePopupStore.getState().requestOpen(id); // null if suppressed
    return () => {
      if (tokenRef.current !== null) {
        usePopupStore.getState().release(tokenRef.current);
        tokenRef.current = null;
      }
    };
  }, [wantOpen, id]);

  return tokenRef.current !== null && activeId === id && activeToken === tokenRef.current;
}
