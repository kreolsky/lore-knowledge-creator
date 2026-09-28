/**
 * Single-active floating-popup slot. Holds at most one open popup at a time.
 *
 * Standalone store (NOT a ui-store slice) because this state is purely ephemeral
 * transient UI — it must never be persisted or trigger preference saves.
 */
// SYSTEM: popups — runtime slot owning the one visible floating popup
// INVARIANT: requestOpen grants only if the slot is free OR the requester's priority
// is >= the active priority (equal => newest wins). A lower-priority request is suppressed
// (returns null). Why: "always show the higher-priority popup; equal => newest" — the rule
// the user stated for floating popups. No queue: release never restores a suppressed popup.

import { create } from 'zustand';
import { POPUP_PRIORITY, type PopupId } from '../components/popup-config';

interface PopupSlotState {
  activeId: PopupId | null;
  activePriority: number;
  activeToken: number; // 0 = no active owner
  _nextToken: number;

  /** Request the slot. Returns a token if granted, or null if suppressed by a higher popup. */
  requestOpen: (id: PopupId) => number | null;
  /** Release a previously-granted token. No-op if the token is stale (already taken over). */
  release: (token: number) => void;
}

export const usePopupStore = create<PopupSlotState>((set, get) => ({
  activeId: null,
  activePriority: 0,
  activeToken: 0,
  _nextToken: 1,

  requestOpen: (id) => {
    const prio = POPUP_PRIORITY[id];
    const { activeId, activePriority, _nextToken } = get();
    if (activeId !== null && prio < activePriority) return null; // suppressed
    set({ activeId: id, activePriority: prio, activeToken: _nextToken, _nextToken: _nextToken + 1 });
    return _nextToken;
  },

  release: (token) => {
    // INVARIANT: only the current owner may free the slot. Why: a popup that already lost
    // the slot to a higher/newer one must not clobber the newer owner when it unmounts.
    if (token !== get().activeToken) return;
    set({ activeId: null, activePriority: 0, activeToken: 0 });
  },
}));
