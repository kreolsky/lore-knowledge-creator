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
import type { AppState } from '../app-store';

export type SessionSlice = Pick<
  AppState,
  | 'currentUser'
  | 'users'
  | 'projects'
  | 'pinLocked'
  | 'toast'
  | 'setUsers'
  | 'setProjects'
  | 'setCurrentUser'
  | 'setPinLocked'
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
        set({ currentUser: null, pinLocked: false });
      } else {
        set({ currentUser: user });
      }
    },
    setPinLocked: (locked) => set({ pinLocked: locked }),

    showToast: (message, type = 'error', options) =>
      set({ toast: { message, type, persistent: options?.persistent } }),
    clearToast: () => set({ toast: null }),
  };
}
