/** Public-share slice — anonymous /s/:token mode flag + owning project name.

# ARCH (debt-paydown W6): first ui-store slice extracted via the chat-store slice-factory
# pattern (spread into one store — useUIStore stays the single hook, so NO selector
# migration). Chosen as the first slice because it is fully TRANSIENT: isPublicShare and
# publicProjectName are NOT in PersistedUIState and neither setter calls triggerSaveUI —
# zero persisted-state surface, the risk the plan flagged to watch.
*/
import { registerLogoutHandler } from '../logout-handlers';
import type { UIState } from '../ui-store';

export type PublicShareSlice = Pick<
  UIState,
  'isPublicShare' | 'publicProjectName' | 'setPublicShare' | 'setPublicProjectName'
  | 'publicFallbackDocIds' | 'markPublicFallback'
>;

type UISet = (
  partial: Partial<UIState> | ((state: UIState) => Partial<UIState>),
) => void;

export function createPublicShareSlice(set: UISet): PublicShareSlice {
  // INVARIANT: publicFallbackDocIds is USER-SCOPED and must not survive a soft
  // logout. Why: the ids record what THIS user's session was refused; a same-tab
  // re-login (shared machine) can be a project member, for whom a stale id would
  // serve the read-only public view instead of the editor — silent wrong UX with
  // no error surface. Registered inside the factory (not at module scope like
  // chat-store) because the slice has no cycle-free access to useUIStore itself;
  // the store is created exactly once, so the closure registers exactly once.
  registerLogoutHandler(() => set({ publicFallbackDocIds: new Set() }));
  return {
    // Public-share mode off by default; only /s/:token flips it on (setPublicShare).
    isPublicShare: false,
    publicProjectName: null,
    setPublicShare: (v) => set({ isPublicShare: v }),
    setPublicProjectName: (name) => set({ publicProjectName: name }),
    publicFallbackDocIds: new Set<string>(),
    markPublicFallback: (ids) => set((state) => {
      const next = new Set(state.publicFallbackDocIds);
      for (const id of ids) next.add(id);
      return { publicFallbackDocIds: next };
    }),
  };
}
