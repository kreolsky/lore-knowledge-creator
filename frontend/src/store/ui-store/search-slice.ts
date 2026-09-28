/** Search slice — query/results cache + mode.

# ARCH (debt-paydown W6): second ui-store slice. Persists via the store method
# get().saveUIStateNow() — NOT the module-level triggerSaveUI — so the slice closes
# over only set/get (no import of the persistence core ⇒ no circular dep, and the
# incident-sensitive triggerSaveUI/buildUIBlob machinery stays untouched in ui-store.ts).
# Persisted fields: searchQuery + searchMode (buildUIBlob); searchResults is transient.
*/
import type { UIState } from '../ui-store';

export type SearchSlice = Pick<
  UIState,
  'searchQuery' | 'searchResults' | 'searchMode' | 'setSearchCache' | 'setSearchMode'
>;

type UISet = (
  partial: Partial<UIState> | ((state: UIState) => Partial<UIState>),
) => void;
type UIGet = () => UIState;

export function createSearchSlice(set: UISet, get: UIGet): SearchSlice {
  return {
    searchQuery: '',
    searchResults: [],
    searchMode: 'fulltext' as const,
    setSearchCache: (query, results) => {
      set({ searchQuery: query, searchResults: results });
      get().saveUIStateNow();
    },
    setSearchMode: (mode) => {
      set({ searchMode: mode });
      get().saveUIStateNow();
    },
  };
}
