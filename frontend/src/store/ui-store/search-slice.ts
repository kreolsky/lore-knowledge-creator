/** Search slice — query/results cache + mode.

# ARCH (debt-paydown W6): second ui-store slice. Persists via the store method
# get().saveUIStateNow() — NOT the module-level triggerSaveUI — so the slice closes
# over only set/get (no import of the persistence core ⇒ no circular dep, and the
# incident-sensitive triggerSaveUI/buildUIBlob machinery stays untouched in ui-store.ts).
# Persisted fields: searchQuery + searchMode + searchDocMode (buildUIBlob); searchResults and searchSnapshot are transient.
*/
import type { UIState } from '../ui-store';
import type { SearchDocMode, SearchMode } from '../../types';
import type { RightTab } from './documents-slice';

export type SearchSlice = Pick<
  UIState,
  'searchQuery' | 'searchResults' | 'searchMode' | 'searchDocMode' | 'searchSnapshot' | 'setSearchCache' | 'setSearchMode'
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
    searchDocMode: 'fulltext' as const,
    searchSnapshot: null,
    setSearchCache: (query, results) => {
      set({ searchQuery: query, searchResults: results });
      get().saveUIStateNow();
    },
    setSearchMode: (mode) => {
      set(mode === 'chats' ? { searchMode: mode } : { searchMode: mode, searchDocMode: mode });
      get().saveUIStateNow();
    },
  };
}

/**
 * The mode the Search tab opens in, picked from the tab it is opened FROM; null
 * keeps the current mode. From Chat ⇒ 'chats'; from any other tab after a chats
 * round ⇒ back to the last document mode.
 */
// WHY remember the document mode instead of resetting to 'fulltext': a chats
// search must not stick to the next search opened from a document tab, and a
// reset would drop the user's chosen semantic mode.
export function searchModeOnOpen(
  fromTab: RightTab | null, mode: SearchMode, docMode: SearchDocMode,
): SearchMode | null {
  if (fromTab === 'chat') return mode === 'chats' ? null : 'chats';
  if (fromTab === 'search') return null;
  return mode === 'chats' ? docMode : null;
}
