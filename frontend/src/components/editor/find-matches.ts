/**
 * Find-match highlighter — decorates EVERY query match, keyed on the search query
 * (not the selection). A CM6 decoration layer for the Find tab (see SYSTEM: editor).
 *
 * WHY a custom plugin: the stock `@codemirror/search` `searchHighlighter` refuses
 * to decorate unless its OWN bottom panel is mounted
 * (`node_modules/@codemirror/search/dist/index.js`: `if (!panel || !query.spec.valid)
 * return Decoration.none`). CodeMirrorEditor deliberately drops `searchKeymap`/
 * `openSearchPanel` and renders a React panel (FindReplacePanel) instead, so `panel`
 * is permanently null and `.cm-searchMatch` never renders. The intermittent
 * highlight users saw was `highlightSelectionMatches()` (`.cm-selectionMatch`) firing
 * after findNext moved the selection — keyed on the selected TEXT, not the query, so
 * it disagreed with case/regexp/whole-word options and stayed dark while typing.
 *
 * The match set lives in `searchMatchesField`, which walks the SAME cursor the
 * Next/Prev/Replace-all commands act on (`SearchQuery.getCursor`) — so the
 * highlighted set is by construction identical to what those commands target,
 * including case/whole-word/regexp semantics. The field re-derives only on query/doc
 * change (never on a bare cursor move), and both the decoration plugin and the
 * Find-panel count read it instead of re-walking. A `.cm-searchMatch-selected` class
 * marks the match under the primary selection (the one findNext just landed on).
 */
import { type EditorState, type Extension, Compartment, RangeSetBuilder, StateField } from '@codemirror/state';
import {
  ViewPlugin, type ViewUpdate, type EditorView, Decoration, type DecorationSet,
} from '@codemirror/view';
import { getSearchQuery, type SearchQuery } from '@codemirror/search';

/** CM6's own cap (matchAll uses 1000). Over it we report `1000+`, never silently
 *  truncating — a partially-highlighted doc that looks complete is the failure mode
 *  this feature removes. */
export const SEARCH_HIGHLIGHT_LIMIT = 1000;

export interface MatchRange {
  from: number;
  to: number;
}

export interface CollectResult {
  ranges: MatchRange[];
  truncated: boolean;
}

/**
 * Pure: collect every match of `query` in `state.doc`, capped at `limit`.
 *
 * Uses `SearchQuery.getCursor` (the public wrapper over the internal Query used by
 * findNext/replaceAll), so the result set is identical to what the Find commands act
 * on — no separate case/whole-word/regexp logic to drift. Zero-length matches (e.g.
 * `a*`) are skipped: the cursor's `.next()` advances past them (no infinite loop),
 * and an empty `{from,to}` decoration is invisible anyway.
 */
export function collectMatches(
  state: EditorState,
  query: SearchQuery,
  limit: number = SEARCH_HIGHLIGHT_LIMIT,
): CollectResult {
  // query.valid is false for an empty search string OR a syntactically-invalid regexp.
  if (!query.valid) return { ranges: [], truncated: false };
  const ranges: MatchRange[] = [];
  let truncated = false;
  const cursor = query.getCursor(state);
  for (;;) {
    const step = cursor.next();
    if (step.done) break;
    const { from, to } = step.value;
    if (from === to) continue; // zero-length (e.g. `a*`) — no visible mark
    if (ranges.length >= limit) { truncated = true; break; }
    ranges.push({ from, to });
  }
  return { ranges, truncated };
}

const matchMark = Decoration.mark({ class: 'cm-searchMatch' });
const selectedMark = Decoration.mark({ class: 'cm-searchMatch cm-searchMatch-selected' });

/**
 * Single source of truth for the match set: derived from the query + doc ONLY (not the
 * selection), so a bare cursor move never triggers a full-document `getCursor` walk.
 * Both the decoration plugin and the Find-panel count read this field instead of
 * re-walking — one scan per query/doc change, shared by both consumers. */
export const searchMatchesField = StateField.define<CollectResult>({
  create(state) {
    return collectMatches(state, getSearchQuery(state));
  },
  update(value, tr) {
    if (tr.docChanged || !getSearchQuery(tr.startState).eq(getSearchQuery(tr.state))) {
      return collectMatches(tr.state, getSearchQuery(tr.state));
    }
    return value;
  },
});

function buildDecorations(view: EditorView): DecorationSet {
  // Ranges come from the cached field — no getCursor walk here.
  const { ranges } = view.state.field(searchMatchesField, false) ?? { ranges: [], truncated: false };
  const sel = view.state.selection;
  // matches come ascending + non-overlapping from getCursor → RangeSetBuilder is valid.
  const builder = new RangeSetBuilder<Decoration>();
  for (const m of ranges) {
    // Mirrors the stock highlighter: the match is "selected" when a selection range
    // equals it exactly (findNext/prev set the selection to the match range).
    const selected = sel.ranges.some(r => r.from === m.from && r.to === m.to);
    builder.add(m.from, m.to, selected ? selectedMark : matchMark);
  }
  return builder.finish();
}

/**
 * Selection-aware decoration plugin. Reads the cached match ranges from
 * searchMatchesField and rebuilds only the (cheap) decoration RangeSet + the
 * `-selected` class on query/doc/selection change — the expensive `getCursor` walk
 * lives in the field, run once per query/doc change (never on a bare cursor move).
 * Unlike the stock highlighter it covers the WHOLE doc (capped), not `visibleRanges`,
 * so off-screen matches are highlighted too. */
export const searchHighlightPlugin = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  constructor(view: EditorView) { this.decorations = buildDecorations(view); }
  update(u: ViewUpdate) {
    if (u.docChanged || u.selectionSet || !getSearchQuery(u.startState).eq(getSearchQuery(u.state))) {
      this.decorations = buildDecorations(u.view);
    }
  }
}, { decorations: v => v.decorations });

/** Bundle: the match-set field + the decoration plugin. Include this wherever the
 *  search() StateField lives. */
export const searchHighlight: Extension = [searchMatchesField, searchHighlightPlugin];

/**
 * Compartment FindReplacePanel reconfigures with an `updateListener` on mount (and
 * clears on unmount), so the match-count / current-index readout stays live as the
 * query, doc, or selection changes. Kept inert ([]) in every editor that does not
 * host a Find panel (mini editors never reconfigure it). Why a Compartment: it is the
 * only leak-free way to attach then REMOVE an updateListener from a view the panel
 * does not own (appendConfig cannot be surgically reversed).
 */
export const searchPanelListenerCompartment = new Compartment();
