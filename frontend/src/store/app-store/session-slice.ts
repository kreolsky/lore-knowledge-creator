/** Session slice — the signed-in user, the directory lists (users/projects), and toasts.

# ARCH (debt-paydown W6): `toast` lives with the session rather than in its own slice
# because `showToast` is the store's error channel — every other slice and the sibling-store
# reset path call it, so it must exist wherever the store does, not behind a feature slice.
#
# NOT here: `setCurrentProject` (the project-switch transition, owned by app-store.ts) and
# `accessLevel` (written by that transition).
*/
import { clearLastSavedBlobs } from '../ui-store';
import { clearUserScopedCaches } from '../logout-handlers';
import { sendTelemetry } from '../../telemetry/telemetry';
import type { AppState } from '../app-store';

export type SessionSlice = Pick<
  AppState,
  | 'currentUser'
  | 'users'
  | 'projects'
  | 'pinLocked'
  | 'unlockedDocIds'
  | 'pageLockLeftAt'
  | 'stampPageLockLeft'
  | 'clearPageLockLeft'
  | 'toast'
  | 'setUsers'
  | 'setProjects'
  | 'setCurrentUser'
  | 'setPinLocked'
  | 'addUnlockedDocId'
  | 'setUnlockedDocIds'
  | 'showToast'
  | 'clearToast'
>;

type AppSet = (
  partial: Partial<AppState> | ((state: AppState) => Partial<AppState> | AppState),
) => void;

export function createSessionSlice(set: AppSet): SessionSlice {
  return {
    currentUser: null,
    users: [],
    projects: [],
    // INVARIANT(security): must clear on logout. Why: a leftover pin lock would lock the next user out of the shared client.
    pinLocked: false,
    // INVARIANT(security): cleared on logout and on the app PIN lock. Why: a page opened
    // by PIN must not stay open for whoever finds the tab locked or signed out.
    unlockedDocIds: [],
    // INVARIANT(security): cleared on logout and on the app PIN lock, inline with
    // unlockedDocIds above. Why: a stamp only WIDENS what unlockedDocIds already exposes,
    // so it must never outlive that set's clears. This is the slice's OWN state — no
    // logout-handler registration (the registry is for other modules' caches).
    pageLockLeftAt: {},
    toast: null,

    setUsers: (users) => set({ users }),
    setProjects: (projects) => set({ projects }),

    // INVARIANT(security): setCurrentUser(null) is the SOFT-LOGOUT entry point — it must drop every
    // user-scoped cache, not just the user pointer.
    // Why: the tab survives a logout, so a same-tab re-login into a shared project would
    // otherwise flash the previous user's lists and saved UI blobs.
    // Adding a new user-scoped cache means registering it via registerLogoutHandler, NOT
    // adding a line here — clearUserScopedCaches fires the whole registry.
    setCurrentUser: (user) => {
      if (!user) {
        clearLastSavedBlobs();
        clearUserScopedCaches();
        set({ currentUser: null, pinLocked: false, unlockedDocIds: [], pageLockLeftAt: {} });
      } else {
        set({ currentUser: user });
      }
    },
    setPinLocked: (locked) => {
      // WHY: the lock replaces the whole tree, tearing down the collab provider — the
      // row names that trigger next to the provider's own `disconnect`.
      sendTelemetry({ category: 'collab', kind: 'pin-lock', detail: { locked } });
      set(locked ? { pinLocked: true, unlockedDocIds: [], pageLockLeftAt: {} } : { pinLocked: false });
    },
    // PIN success is presence: besides opening the id, it ends the id's grace window
    // (a re-PIN after a lapsed window must open the page — the stamp never survives it).
    addUnlockedDocId: (docId) => set(s => {
      const hasId = s.unlockedDocIds.includes(docId);
      const hasStamp = s.pageLockLeftAt[docId] !== undefined;
      if (hasId && !hasStamp) return {};
      const pageLockLeftAt = { ...s.pageLockLeftAt };
      delete pageLockLeftAt[docId];
      return hasId ? { pageLockLeftAt } : { unlockedDocIds: [...s.unlockedDocIds, docId], pageLockLeftAt };
    }),
    setUnlockedDocIds: (docIds) => set({ unlockedDocIds: docIds }),
    // Stamp-if-ABSENT (the guard lives here, not in callers): a later navigation must not
    // freshen an already-running window — the window counts from the FIRST leave.
    stampPageLockLeft: ids => set(s => {
      let next: Record<string, number> | null = null;
      for (const id of ids) {
        if (s.pageLockLeftAt[id] !== undefined) continue;
        if (!next) next = { ...s.pageLockLeftAt };
        next[id] = Date.now();
      }
      return next ? { pageLockLeftAt: next } : {};
    }),
    clearPageLockLeft: ids => set(s => {
      let next: Record<string, number> | null = null;
      for (const id of ids) {
        if (s.pageLockLeftAt[id] === undefined) continue;
        if (!next) next = { ...s.pageLockLeftAt };
        delete next[id];
      }
      return next ? { pageLockLeftAt: next } : {};
    }),

    showToast: (message, type = 'error', options) =>
      set({ toast: { message, type, persistent: options?.persistent } }),
    clearToast: () => set({ toast: null }),
  };
}
