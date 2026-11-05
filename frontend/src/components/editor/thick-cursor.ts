/**
 * Thick cursor layer — draws the primary caret as a DOM rectangle of configurable
 * width (2–3px), leaving text selection to native browser ::selection.
 *
 * ARCH: replaces @codemirror/view's drawSelection() which bundles cursor + selection
 * rendering and hides native ::selection. We need thick cursor but compositor-level
 * selection (visible over .cm-line backgrounds in code blocks / inline code).
 * Uses public layer() + RectangleMarker API from @codemirror/view v6.
 *
 * INVARIANT: three rules must hold — regression in any breaks cursor rendering:  Why: the caret is a synthetic DOM overlay (not a real selection); the three rules below keep it aligned, and a regression in any breaks cursor rendering.
 *   1. Caret is rendered via `border-left` on a zero-width RectangleMarker,
 *      NOT via a background-painted rectangle. CSS border widths are snapped
 *      to device pixels, ensuring uniform thickness at fractional devicePixelRatio.
 *   2. `caretColor: 'transparent'` on `.cm-content` hides the native caret.
 *      Without this, two carets appear simultaneously.
 *   3. `drawSelection()` is NEVER imported or included in any extension array.
 *      Selection is rendered by the browser's native ::selection pseudo-element,
 *      which paints over element backgrounds (visible inside code blocks / inline code).
 */

import { StateEffect, StateField } from '@codemirror/state';
import { EditorView, layer, RectangleMarker, ViewPlugin, type ViewUpdate } from '@codemirror/view';

// WHY border-left (not background width): at fractional devicePixelRatio
// (Windows display scaling, zoomed Retina), a CSS background rectangle of N
// px maps to a fractional number of physical pixels and the browser smears
// the edges with partial opacity — visible as inconsistent cursor thickness
// at different caret positions. CSS border widths are snapped to device
// pixels by the browser, so we render the caret as a 2px border-left on a
// zero-width RectangleMarker. This is the same trick @codemirror/view's own
// drawSelection() uses for .cm-cursor.
const CURSOR_WIDTH = 0;

// A caret rect is only usable if it has real height; a collapsed/atomic boundary can
// yield a zero-height rect that would draw an invisible caret. null → caller tries the
// next side bias.
type CaretRect = { left: number; top: number; bottom: number } | null;
const validCoords = (c: CaretRect): CaretRect => (c && c.bottom - c.top > 0 ? c : null);

// WHY: restart blink animation on every cursor move so the cursor is always
// immediately visible when placed. The layer reuses the existing DOM element, moved
// (marker.update) or left alone (marker.eq), so the blink phase would carry over.
const cursorBlinkReset = ViewPlugin.fromClass(class {
  update(update: ViewUpdate) {
    if (!update.selectionSet && !update.focusChanged) return;
    // INVARIANT: the reset addresses this view's OWN layer, a direct child of its scroller.
    // Why: a mounted table cell is a nested editor with its own layer, earlier in the DOM,
    // so a plain query reset the cell and the document's caret moved mid-blink, hidden.
    const layerEl = update.view.scrollDOM.querySelector(':scope > .cm-thick-cursor-layer');
    if (!layerEl) return;
    for (const el of layerEl.children) {
      const htmlEl = el as HTMLElement;
      htmlEl.style.animation = 'none';
      void htmlEl.offsetWidth; // force reflow to reset animation timeline
      htmlEl.style.animation = '';
    }
  }
});

/**
 * `true` pauses the caret, `false` resumes it and redraws at the current coordinates.
 * WHY: a widget can move the edge the caret stands at with no CM6 change: a table resize
 * writes widths to Yjs, so nothing else tells this layer its marker went stale.
 */
export const caretHold = StateEffect.define<boolean>();

const caretHeld = StateField.define<boolean>({
  create: () => false,
  update: (held, tr) => tr.effects.reduce((value, e) => (e.is(caretHold) ? e.value : value), held),
});

export const thickCursorLayer = [
  caretHeld,
  layer({
    above: true,
    class: 'cm-thick-cursor-layer',
    update: update =>
      update.docChanged ||
      update.selectionSet ||
      update.viewportChanged ||
      update.geometryChanged ||
      update.focusChanged ||
      update.transactions.some(tr => tr.effects.some(e => e.is(caretHold))),
    markers: view => {
      if (!view.hasFocus || view.state.field(caretHeld)) return [];
      const markers: RectangleMarker[] = [];
      const rect = view.scrollDOM.getBoundingClientRect();
      for (const range of view.state.selection.ranges) {
        if (!range.empty) continue;
        // WHY the side fallback: at a Decoration.replace / atomic boundary (e.g. the caret
        // left just inside a rendered link, table anchor, math/mermaid block, or any
        // collapsed mark) coordsAtPos with the default side resolves *into* the replaced
        // range and returns null or a zero-height rect — the caret then vanishes. Retry
        // biased to the char before (-1) then after (+1) to land on a real glyph edge.
        // Shared layer, so this also hardens the main document editor, not just cells.
        const coords = validCoords(view.coordsAtPos(range.head))
          ?? validCoords(view.coordsAtPos(range.head, -1))
          ?? validCoords(view.coordsAtPos(range.head, 1));
        if (!coords) continue;
        // WHY round: coordsAtPos returns fractional px because glyph advance widths
        // are subpixel. A 2px-wide rectangle positioned at a fractional x is split
        // across 3 physical pixels with partial opacity, making the cursor visibly
        // thinner/blurrier at some caret positions and crisp at others. Snapping to
        // integer pixels yields a uniform thickness regardless of preceding chars.
        const left = Math.round(coords.left - rect.left + view.scrollDOM.scrollLeft);
        const top = Math.round(coords.top - rect.top + view.scrollDOM.scrollTop);
        const height = Math.round(coords.bottom - coords.top);
        markers.push(new RectangleMarker('cm-thick-cursor', left, top, CURSOR_WIDTH, height));
      }
      return markers;
    },
  }),
  cursorBlinkReset,
  EditorView.theme({
    '.cm-content': { caretColor: 'transparent' },
    '.cm-thick-cursor': {
      pointerEvents: 'none',
      borderLeft: '3px solid var(--accent)',
      boxSizing: 'content-box',
      animation: 'cm-thick-cursor-blink 1.2s steps(1) infinite',
    },
    '@keyframes cm-thick-cursor-blink': {
      '0%': {},
      '50%': { visibility: 'hidden' },
      '100%': {},
    },
  }),
];
