/** CM6 plugins for Editor: heading highlight, bare URL marks, link click handler. */
// SYSTEM: editor-plugins — CM6 plugin collection (heading highlight, bare URL marks, link click handler)
// ARCH: Link-type marks (cm-note-link, cm-ref-link, cm-doc-link, cm-ext-link) are added
// inside livePreviewField's buildStructuralDecorations — same decoration set as the
// Decoration.replace that hides markdown syntax. This ensures marks compose correctly
// with replaces (ViewPlugin marks don't compose across decoration sources).
// This plugin only handles bare URLs (not parsed as Link nodes by lezer-markdown).

import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import { EditorView, Decoration, DecorationSet, ViewPlugin, ViewUpdate } from '@codemirror/view';
import { RangeSetBuilder, Compartment } from '@codemirror/state';
import { bareUrl } from '../components/editor/link-patterns';
import { isViewportCovered, revealAtCursor } from '../components/editor/live-preview';
import { collectCodeRanges, makeInCodeChecker, isPosInCode } from '../components/editor/code-range-utils';
import { LINK_TYPES, showBrokenToast } from '../components/editor/live-preview/link-types';
import { getRoleView } from './active-editor';
import { useUIStore } from '../store/ui-store';
import { refIsScope } from '../store/ui-store/documents-slice';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';

/**
 * Is this EditorView the panel-mode quick-preview editor (the reference rendered
 * inside the Refs tab)? The ONE view-identity check behind the note gating: a
 * 'note:' link click and the toolbar note button are suppressed there (notes are
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
}
const preclickGesture = new WeakMap<EditorView, PreclickGesture>();

export const linkClickExtension = EditorView.domEventHandlers({
  mousedown: (event, view) => {
    preclickGesture.set(view, {
      head: view.state.selection.main.head,
      x: event.clientX,
      y: event.clientY,
    });
    return false;
  },
  click: (event, view) => {
    // INVARIANT: a drag/selection gesture is not a click — gate BEFORE any link matching (and before posAtCoords), returning false without preventDefault so CM6's native selection behavior keeps running.
    // Why: a drag returning to its start point ends with an empty selection (only the px
    // threshold catches it); a double/triple-click or shift+extend ends with a non-empty
    // selection (only the selection check catches it). Cmd/Ctrl remain navigate-intent
    // modifiers but do not bypass the gate.
    const gesture = preclickGesture.get(view) ?? null;
    if (event.button !== 0 || event.shiftKey || event.altKey) return false;
    if (gesture !== null && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) > DRAG_THRESHOLD_PX) return false;
    const selection = view.state.selection;
    if (!selection.main.empty || selection.ranges.length > 1) return false;

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
      if (!event.metaKey && !event.ctrlKey && !isExpanded) return false;
      action();
      event.preventDefault();
      return true;
    };

    // Registry-driven: iterate each link type's normalized matches in priority order.
    for (const entry of LINK_TYPES) {
      for (const m of entry.match(lineText)) {
        const handled = tryLink(m.start, m.end, () => {
          if (entry.validate(m.id)) {
            // Note links in the panel quick preview: 'open-notes' would swap the
            // Refs tab away and hide the anchor — decision A has no notes on the
            // preview, so toast instead of acting. Every other kind acts normally.
            if (entry.kind === 'note' && isPanelPreviewView(view)) {
              noteUnavailableInPreviewToast();
            } else {
              entry.action(m.id);
            }
          } else {
            showBrokenToast(entry.decorationClass);
          }
        });
        if (handled) return true;
      }
    }

    return false;
  }
});

// ARCH: Module-level Compartment for dynamic readonly toggling. CM6 facets set
// via `.of()` are static; Compartment allows reconfiguration without recreating
// the entire extension array.
export const editableCompartment = new Compartment();
