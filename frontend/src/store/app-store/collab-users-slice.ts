/** Collab-users slice — presence chips for the active collab entity.

# ARCH (debt-paydown W6): first app-store slice, mirroring the ui-store/chat-store split.
# The slice closes over `set` only — presence is pure list state with no cross-store reads.
# `collabUsers` is reset on entity switch by setCurrentDocument/setCurrentReference in
# app-store.ts, which stay the owners of the switch sequence: a slice must not reach into
# another slice's transition.
*/
import type { AppState } from '../app-store';

export type CollabUsersSlice = Pick<
  AppState,
  'collabUsers' | 'setCollabUsers' | 'addCollabUser' | 'removeCollabUser'
>;

type AppSet = (
  partial: Partial<AppState> | ((state: AppState) => Partial<AppState> | AppState),
) => void;

export function createCollabUsersSlice(set: AppSet): CollabUsersSlice {
  return {
    collabUsers: [],
    setCollabUsers: (users) => set({ collabUsers: users }),
    // WHY: de-duplicated by user_id, and a duplicate join returns the SAME state
    // object (`s`, not a rebuilt array). Why: presence chips re-render on every peer
    // heartbeat otherwise — a new array identity for an unchanged list defeats Zustand's
    // reference equality and repaints the chip row on each duplicate join frame.
    addCollabUser: (user) => set((s) =>
      s.collabUsers.some(u => u.user_id === user.user_id)
        ? s
        : { collabUsers: [...s.collabUsers, user] }),
    removeCollabUser: (userId) => set((s) => ({
      collabUsers: s.collabUsers.filter(u => u.user_id !== userId),
    })),
  };
}
