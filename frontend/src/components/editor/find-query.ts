/** Pure builder mapping FindReplacePanel local state to a CM6 SearchQuery. */
// WHY: extracted as a pure function so the state→SearchQuery mapping is testable
// without a live EditorView (the panel's CM6 command wiring is manual-test only).

import { SearchQuery } from '@codemirror/search';

export interface FindState {
  search: string;
  replace: string;
  caseSensitive: boolean;
  regexp: boolean;
  wholeWord: boolean;
}

export function buildSearchQuery(state: FindState): SearchQuery {
  return new SearchQuery({
    search: state.search,
    replace: state.replace,
    caseSensitive: state.caseSensitive,
    regexp: state.regexp,
    wholeWord: state.wholeWord,
  });
}
