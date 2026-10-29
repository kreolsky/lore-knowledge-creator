/** CM6 plugins for Editor: heading highlight, bare URL marks, link click handler. */
// SYSTEM: editor-plugins — CM6 plugin collection (heading highlight, bare URL marks, link click handler)
// ARCH: Link-type marks (cm-ref-link, cm-doc-link, cm-ext-link) are added
// inside livePreviewField's buildStructuralDecorations — same decoration set as the
// Decoration.replace that hides markdown syntax. This ensures marks compose correctly
// with replaces (ViewPlugin marks don't compose across decoration sources).
// This plugin only handles bare URLs (not parsed as Link nodes by lezer-markdown).

import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { EditorView, Decoration, DecorationSet, ViewPlugin, ViewUpdate } from '@codemirror/view';
import { RangeSetBuilder, Compartment, EditorSelection, type EditorState } from '@codemirror/state';
import { bareUrl } from '../components/editor/link-patterns';
import { isViewportCovered, revealAtCursor } from '../components/editor/live-preview';
import { collectCodeRanges, makeInCodeChecker, isPosInCode } from '../components/editor/code-range-utils';
import { LINK_TYPES, onBrokenLinkClick, isEmbedMatch } from '../components/editor/live-preview/link-types';
import { widgetClickPos } from '../components/editor/live-preview/table-widget';
import { getRoleView } from './active-editor';
import { useUIStore } from '../store/ui-store';
import { refIsScope } from '../store/ui-store/documents-slice';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';

/**
 * Is this EditorView the panel-mode quick-preview editor (the reference rendered
 * inside the Refs tab)? The ONE view-identity check behind the note gating: a
 * note-anchor click and the toolbar note button are suppressed there (notes are
 * document-scoped in preview — decision A of the panel quick-preview plan). Split
 * mode's secondary column answers false: showsBothPanes it may be, but the
 * reference IS the scope there, so notes on it stay allowed.
 */
export function isPanelPreviewView(view: EditorView): boolean {
  // Mode of the current document; 'center' when no doc is open (no preview exists).
  const docId = useAppStore.getState().currentDocument?.document_id ?? '';
  const mode = useUIStore.getState().getRefOpenMode(docId);
  if (refIsScope(mode)) return false;
  return getRoleView('secondary') === view;
}

/** No-silent-degradation toast for note affordances suppressed in quick preview. */
export function noteUnavailableInPreviewToast(): void {
  useAppStore.getState().showToast(t('noteUnavailableInPreview'), 'info');
}

// ─── Heading highlight — maps tags.heading1-6 to semantic CSS classes ─────────
export const headingHighlight = syntaxHighlighting(
  HighlightStyle.define([
    { tag: tags.heading1, class: 'cm-header-1' },
    { tag: tags.heading2, class: 'cm-header-2' },
    { tag: tags.heading3, class: 'cm-header-3' },
    { tag: tags.heading4, class: 'cm-header-4' },
    { tag: tags.heading5, class: 'cm-header-5' },
    { tag: tags.heading6, class: 'cm-header-6' },
    { tag: tags.strong, class: 'cm-strong' },
    { tag: tags.emphasis, class: 'cm-em' },
    { tag: tags.monospace, class: 'cm-inline-code' },
    { tag: tags.strikethrough, class: 'cm-strikethrough' },
  ]),
);

// ─── Bare URL plugin ────────────────────────────────────────────────────────
export const bareUrlPlugin = ViewPlugin.fromClass(class {
  decorations: DecorationSet;
  lastViewport: { from: number; to: number }[] = [];
  constructor(view: EditorView) {
    this.decorations = this.buildDecorations(view);
    this.lastViewport = view.visibleRanges.map((r: { from: number; to: number }) => ({ from: r.from, to: r.to }));
  }
  update(update: ViewUpdate) {
    if (update.docChanged) {
      this.decorations = this.buildDecorations(update.view);
      this.lastViewport = update.view.visibleRanges.map((r: { from: number; to: number }) => ({ from: r.from, to: r.to }));
      return;
    }
    if (update.viewportChanged && !isViewportCovered(this.lastViewport, update.view.visibleRanges)) {
      this.decorations = this.buildDecorations(update.view);
      this.lastViewport = update.view.visibleRanges.map((r: { from: number; to: number }) => ({ from: r.from, to: r.to }));
    }
  }
  buildDecorations(view: EditorView) {
    const builder = new RangeSetBuilder<Decoration>();
    const codeRanges = collectCodeRanges(view.state, view.visibleRanges);
    const inCode = makeInCodeChecker(codeRanges);
    const matches: { start: number, end: number }[] = [];

    for (const { from, to } of view.visibleRanges) {
      const text = view.state.doc.sliceString(from, to);
      const regex = bareUrl();
      regex.lastIndex = 0;
      let match;
      while ((match = regex.exec(text)) !== null) {
        const start = from + match.index;
        const end = start + match[0].length;
        if (inCode(start, end)) continue;
        matches.push({ start, end });
      }
    }

    matches.sort((a, b) => a.start - b.start);
    for (const m of matches) {
      const url = view.state.doc.sliceString(m.start, m.end);
      builder.add(m.start, m.end, Decoration.mark({
        class: 'cm-ext-link',
        attributes: {
          'data-link-type': 'ext',
          'data-link-url': url,
        }
      }));
    }
    return builder.finish();
  }
}, {
  decorations: v => v.decorations
});

// ─── Link click handler ─────────────────────────────────────────────────────

// INVARIANT: mousedown and click are dispatched by the SAME per-view domEventHandlers
// instance, so `map.get(view)` in click always reads the value written by the matching mousedown — there is no cross-view mousedown→click sequence.
// Why: mousedown+click share one per-view handler instance, so click's map.get(view) reads the mousedown's value; a cross-view sequence would break that assumption.
// If one were ever introduced, the WeakMap would correctly yield `undefined` (= "no
// gesture data: click passes the gate, link treated as not expanded") rather than a
// stale value from another view. Fixes a split-view race that a module-level variable had.

/** Pointer travel between mousedown and click above which the gesture is a drag, not a
 *  click. Jitter tolerance for a hand holding still; lower it if single-character
 *  drag-selects start misfiring as clicks. */
const DRAG_THRESHOLD_PX = 4;

/** Gesture snapshot taken at mousedown; read back at click to reject non-click gestures. */
interface PreclickGesture {
  head: number;
  x: number;
  y: number;
  /** Source position of a press inside a rendered table, mapped while its DOM is attached. */
  widgetPos: number | null;
  /** Set when the mousedown was claimed from CM6 as a link click (selection untouched):
   *  the doc position of that link. */
  linkPos: number | null;
}
const preclickGesture = new WeakMap<EditorView, PreclickGesture>();

type LinkEntry = (typeof LINK_TYPES)[number];
type LinkMatch = ReturnType<LinkEntry['match']>[number];

/** The registry link covering `pos` (`start <= offset < end`), or null — none inside code. */
function linkAt(state: EditorState, pos: number): { entry: LinkEntry; match: LinkMatch } | null {
  if (isPosInCode(state, pos)) return null;
  const line = state.doc.lineAt(pos);
  const offset = pos - line.from;
  for (const entry of LINK_TYPES) {
    for (const match of entry.match(line.text)) {
      // Embeds never claim/navigate — the gesture belongs to the rendered widget.
      if (isEmbedMatch(line.text, match)) continue;
      if (offset >= match.start && offset < match.end) return { entry, match };
    }
  }
  return null;
}

function followLink({ entry, match }: { entry: LinkEntry; match: LinkMatch }): void {
  if (entry.validate(match.id)) entry.action(match.id);
  else onBrokenLinkClick(entry, match.id);
}

// Same platform test CM6 uses for its "add selection range" modifier.
const IS_MAC = typeof navigator !== 'undefined' && /Mac/.test(navigator.platform);

const linkClickHandlers = EditorView.domEventHandlers({
  mousedown: (event, view) => {
    // Mapped FIRST: CM6's own mousedown may reveal the table and detach its DOM.
    const widgetPos = widgetClickPos(view, event);
    const gesture: PreclickGesture = {
      head: view.state.selection.main.head,
      x: event.clientX,
      y: event.clientY,
      widgetPos,
      linkPos: null,
    };
    preclickGesture.set(view, gesture);
    if (event.button !== 0 || event.shiftKey || event.altKey) return false;
    const modKey = IS_MAC ? event.metaKey : event.ctrlKey;
    if (!modKey && widgetPos === null) return false;
    let pos = widgetPos;
    if (pos === null) {
      try { pos = view.posAtCoords({ x: event.clientX, y: event.clientY }); }
      catch { return false; }
    }
    // INVARIANT: Mod+click on a link, and any press on a link in a rendered table, is claimed here — CM6's mouse selection never runs, so no selection range is added and the table does not reveal.
    // Why: CM6 adds the Mod+click range on mousedown, and the click gate then rejects a
    // multi-range selection, so navigation was unreachable; a reveal re-flows the table
    // under the pointer before the click arrives.
    if (pos === null || linkAt(view.state, pos) === null) return false;
    gesture.linkPos = pos;
    event.preventDefault();
    return true;
  },
  click: (event, view) => {
    // INVARIANT: a drag/selection gesture is not a click — gate BEFORE any link matching (and before posAtCoords), returning false without preventDefault so CM6's native selection behavior keeps running.
    // Why: a drag returning to its start point ends with an empty selection (only the px
    // threshold catches it); a triple-click or shift+extend ends with a non-empty
    // selection (only the selection check catches it). A Mod+click on a link passes it
    // because its mousedown was claimed above and never touched the selection.
    const gesture = preclickGesture.get(view) ?? null;
    if (event.button !== 0 || event.shiftKey || event.altKey) return false;
    if (gesture !== null && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > DRAG_THRESHOLD_PX) return false;
    // A claimed press or a press in a rendered table: the position was fixed at mousedown —
    // posAtCoords would now read the re-flowed raw text. A double-click's second click
    // never navigates again.
    if (gesture !== null && (gesture.linkPos !== null || gesture.widgetPos !== null)) {
      if (event.detail > 1) return false;
      const link = gesture.linkPos !== null ? linkAt(view.state, gesture.linkPos) : null;
      if (link) {
        followLink(link);
        event.preventDefault();
        return true;
      }
      // A non-link press in a table: the caret was placed by tableMouseSelection.
      return false;
    }
    // INVARIANT: a double-click whose first click revealed a link navigates; the word
    // selection CM6 made on its second mousedown does not count as a selection gesture.
    // Why: an already-expanded link navigates on the first click, so a double-click on a
    // link only ever means "reveal, then open" — rejecting it forced a slow third click.
    const isDoubleClick = event.detail === 2;
    const selection = view.state.selection;
    if (selection.ranges.length > 1 || (!selection.main.empty && !isDoubleClick)) return false;

    let pos: number | null;
    try { pos = view.posAtCoords({ x: event.clientX, y: event.clientY }); }
    catch { return false; }
    if (pos === null) return false;

    if (isPosInCode(view.state, pos)) return false;

    const line = view.state.doc.lineAt(pos);
    const lineText = line.text;
    const offset = pos - line.from;
    const preclick = gesture?.head ?? null;
    // revealAtCursor=false ⇒ read-only view with no raw-markdown reveal step, so the
    // editable expand-then-navigate dance is meaningless: a plain click = navigate.
    const revealOn = view.state.facet(revealAtCursor);

    const wasExpanded = (matchStart: number, matchEnd: number) =>
      preclick !== null &&
      preclick > line.from + matchStart &&
      preclick < line.from + matchEnd;

    const tryLink = (start: number, end: number, action: () => void): boolean => {
      if (!revealOn) {
        // The first click of the double-click already navigated.
        if (isDoubleClick) return false;
        if (!(offset >= start && offset < end)) return false;
        action();
        event.preventDefault();
        return true;
      }
      const isExpanded = wasExpanded(start, end);
      const inClickZone = isExpanded
        ? (offset > start && offset < end)
        : (offset >= start && offset < end);
      if (!inClickZone) return false;
      const navigateIntent = isDoubleClick ? isExpanded : (isExpanded || event.metaKey || event.ctrlKey);
      if (!navigateIntent) return false;
      action();
      event.preventDefault();
      return true;
    };

    // Registry-driven: iterate each link type's normalized matches in priority order.
    // Embeds (`![…](…)`) are skipped — an embed click never navigates; the gesture
    // belongs to the rendered widget (image selects its node, the transclusion band
    // header keeps its own open-source affordance).
    for (const entry of LINK_TYPES) {
      for (const m of entry.match(lineText)) {
        if (isEmbedMatch(lineText, m)) continue;
        const handled = tryLink(m.start, m.end, () => followLink({ entry, match: m }));
        if (handled) return true;
      }
    }

    return false;
  }
});

/**
 * Mouse selection that starts inside a rendered GFM table: anchored at the clicked cell's
 * source position instead of the widget edge CM6 would resolve. WHY a selection style, not a
 * click-time fix: the press reveals the raw table, the browser then drops the `click` whose
 * mousedown target was detached, and a drag would still start at the table's first char.
 */
const tableMouseSelection = EditorView.mouseSelectionStyle.of((view, event) => {
  if (event.button !== 0 || event.shiftKey || event.altKey) return null;
  const start = widgetClickPos(view, event);
  if (start === null) return null;
  let anchor = start;
  return {
    get(cur, _extend, multiple) {
      // Within the jitter radius the head stays on the mapped char: posAtCoords would read
      // the re-flowed raw text under a pointer that has not really moved.
      const still = Math.hypot(cur.clientX - event.clientX, cur.clientY - event.clientY) <= DRAG_THRESHOLD_PX;
      const head = still ? anchor : (view.posAtCoords({ x: cur.clientX, y: cur.clientY }) ?? anchor);
      const range = EditorSelection.range(anchor, head);
      return multiple ? view.state.selection.addRange(range) : EditorSelection.create([range]);
    },
    update(update) {
      if (update.docChanged) anchor = update.changes.mapPos(anchor);
    },
  };
});

export const linkClickExtension = [linkClickHandlers, tableMouseSelection];

// ARCH: Module-level Compartment for dynamic readonly toggling. CM6 facets set
// via `.of()` are static; Compartment allows reconfiguration without recreating
// the entire extension array.
export const editableCompartment = new Compartment();
