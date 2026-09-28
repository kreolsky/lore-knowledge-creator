/** Sidebar slice — tab + open state + collapsed-doc ids (project-persisted).

# ARCH (debt-paydown W6): third ui-store slice. Persists via get().saveUIStateNow()
# (not the module-level triggerSaveUI) — slice closes over only set/get, no import of the
# persistence core, so triggerSaveUI / buildUIBlob stay untouched.
*/
import type { UIState } from '../ui-store';

export type SidebarSlice = Pick<
  UIState,
  'sidebarTab' | 'sidebarOpen' | 'collapsedDocIds' | 'setSidebarTab' | 'setSidebarOpen' | 'toggleDocExpanded' | 'expandDocs'
>;

type UISet = (
  partial: Partial<UIState> | ((state: UIState) => Partial<UIState>),
) => void;
type UIGet = () => UIState;

export function createSidebarSlice(set: UISet, get: UIGet): SidebarSlice {
  return {
    sidebarTab: 'docs',
    sidebarOpen: true,
    collapsedDocIds: [],
    setSidebarTab: (tab) => { set({ sidebarTab: tab }); get().saveUIStateNow(); },
    setSidebarOpen: (open) => { set({ sidebarOpen: open }); get().saveUIStateNow(); },
    toggleDocExpanded: (docId) => {
      set(prev => ({
        collapsedDocIds: prev.collapsedDocIds.includes(docId)
          ? prev.collapsedDocIds.filter(id => id !== docId)
          : [...prev.collapsedDocIds, docId]
      }));
      get().saveUIStateNow();
    },
    // WHY: reveal-in-tree expansion ONLY removes ids from collapsedDocIds —
    // it never collapses. Why: reveal is an explicit "show me where this is" gesture;
    // expanding a user-collapsed branch silently is fine, but collapsing one they
    // left open would rewrite persisted state against them. Idempotent: ids already
    // expanded are a no-op. Same persistence path as toggle.
    expandDocs: (docIds) => {
      if (docIds.length === 0) return;
      const drop = new Set(docIds);
      set(prev => ({ collapsedDocIds: prev.collapsedDocIds.filter(id => !drop.has(id)) }));
      get().saveUIStateNow();
    },
  };
}
