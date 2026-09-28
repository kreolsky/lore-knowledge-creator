/**
 * Typographic ligature rendering plugin for CodeMirror 6.
 *
 * Visually replaces character sequences (e.g. `->` → `→`) via Decoration.replace.
 * Document text is unchanged — purely visual. Cursor inside a match reveals source.
 *
 * ARCH: Separate ViewPlugin — all ligatures are single-line, so ViewPlugin is correct
 * per project rules. Independent from livePreviewField to keep concerns separated
 * and allow independent toggling.
 */
// SYSTEM: ligature — visual replacement of character sequences with Unicode glyphs

import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';
import { RangeSetBuilder, type EditorState } from '@codemirror/state';
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from '@codemirror/view';

import { collectCodeRanges, makeInCodeChecker, type CodeRange } from './code-range-utils';
import { isViewportCovered, revealAtCursor } from './live-preview';

// ─── Replacement table ──────────────────────────────────────────────────────
// WHY: sorted longest-first — greedy scan relies on this order.  Why: the greedy scan matches the first (longest) ligature at each offset; without longest-first a short rule would shadow a longer one ('--' before '---').

export interface LigatureRule {
  pattern: string;
  glyph: string;
}

export const LIGATURE_RULES: LigatureRule[] = [
  { pattern: '---', glyph: '\u2014' }, // em-dash
  { pattern: '(c)', glyph: '\u00A9' },  // copyright en
  { pattern: '(с)', glyph: '\u00A9' },  // copyright ru
  { pattern: '<->', glyph: '\u2194' }, // left-right arrow
  { pattern: '--', glyph: '\u2013' },  // en-dash
  { pattern: '->', glyph: '\u2192' },  // right arrow
  { pattern: '<-', glyph: '\u2190' },  // left arrow
  { pattern: '>=', glyph: '\u2265' },  // greater-equal
  { pattern: '<=', glyph: '\u2264' },  // less-equal
];

const TRIGGER_CHARS = new Set(LIGATURE_RULES.map((r) => r.pattern[0]));

// ─── Widget ─────────────────────────────────────────────────────────────────

class LigatureWidget extends WidgetType {
  constructor(readonly glyph: string) {
    super();
  }
  toDOM(): HTMLElement {
    const span = document.createElement('span');
    span.className = 'cm-ligature';
    span.textContent = this.glyph;
    return span;
  }
  eq(other: LigatureWidget): boolean {
    return this.glyph === other.glyph;
  }
  ignoreEvent(): boolean {
    return false;
  }
}

// ─── Excluded ranges (code blocks, HR) ──────────────────────────────────────
// ARCH: HR excluded only when live-preview would replace it (cursor far away).
// When cursor is near, live-preview reveals raw --- so ligature should apply.

function collectExcludedRanges(
  view: EditorView,
): CodeRange[] {
  const ranges = collectCodeRanges(view.state, view.visibleRanges);

  // INVARIANT: reveal-on-cursor is an editing affordance. A read-only nested transclusion
  // view (selection defaults to position 0, revealAtCursor=false) must NOT honor the cursor,
  // else a leading ligature sequence at offset 0 renders raw.  Why: a read-only nested view has an implicit position-0 selection; honoring the cursor there would leave a leading ligature un-rendered, so revealAtCursor=false suppresses it. See effects.ts (revealAtCursor).
  const selRanges = view.state.facet(revealAtCursor) ? view.state.selection.ranges : [];
  const isCursorNear = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(view.state).iterate({
      from,
      to,
      enter(node) {
        if (
          node.name === 'HorizontalRule' &&
          !isCursorNear(node.from, node.to)
        ) {
          ranges.push({ from: node.from, to: node.to });
        }
      },
    });
  }
  return ranges;
}

// ─── Decoration builder ─────────────────────────────────────────────────────

function buildLigatureDecorations(view: EditorView): DecorationSet {
  const excluded = collectExcludedRanges(view);
  // INVARIANT: same revealAtCursor gate as collectExcludedRanges — a nested read-only view
  // never suppresses ligatures based on its implicit position-0 selection.  Why: mirrors collectExcludedRanges — a read-only view's position-0 selection must not be treated as a cursor for ligature suppression.
  const selRanges = view.state.facet(revealAtCursor) ? view.state.selection.ranges : [];

  const inExcluded = (from: number, to: number) =>
    excluded.some((r) => from < r.to && to > r.from);

  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from < to && r.to >= from);

  const builder = new RangeSetBuilder<Decoration>();

  for (const { from, to } of view.visibleRanges) {
    const text = view.state.doc.sliceString(from, to);
    let i = 0;

    while (i < text.length) {
      const ch = text[i];

      if (!TRIGGER_CHARS.has(ch)) {
        i++;
        continue;
      }

      // Skip escaped sequences
      if (i > 0 && text[i - 1] === '\\') {
        i++;
        continue;
      }

      let matched = false;
      for (const rule of LIGATURE_RULES) {
        if (text.startsWith(rule.pattern, i)) {
          const absFrom = from + i;
          const absTo = absFrom + rule.pattern.length;

          if (!inExcluded(absFrom, absTo) && !isCursorIn(absFrom, absTo)) {
            builder.add(
              absFrom,
              absTo,
              Decoration.replace({ widget: new LigatureWidget(rule.glyph) }),
            );
          }

          i += rule.pattern.length;
          matched = true;
          break;
        }
      }

      if (!matched) i++;
    }
  }

  return builder.finish();
}

// ─── Copy-side substitution ─────────────────────────────────────────────────

/**
 * Return the `[from, to]` slice of `state.doc` with ligature sequences substituted to their
 * glyphs — the clipboard counterpart to the on-screen plugin. Pure (no `EditorView`): takes
 * an `EditorState` + range so the copy handler can run it without a view.
 *
 * Same `LIGATURE_RULES` + `TRIGGER_CHARS` and the same exclusions (InlineCode / FencedCode /
 * HorizontalRule) as `buildLigatureDecorations`, but WITHOUT the cursor-reveal gate: copy is
 * deterministic, so every non-excluded match emits its glyph.
 *
 * keep in sync with buildLigatureDecorations — the parity test (ligature-plugin.test.ts,
 * "PARITY: whole-doc glyphs match the live decoration set") is the drift guard.
 *
 * ARCH: in the copy handler this runs on the SOURCE slice (where tables are still
 * `![Table](table:id)` anchors) BEFORE `expandTableAnchorsToGfm`. Reversing the order would
 * substitute the `---` inside a produced GFM delimiter row (`| --- | --- |`) to `—` and
 * corrupt the table. Why: substitution must never see synthesized table markdown.
 */
export function ligatureSubstitutedText(
  state: EditorState,
  from: number,
  to: number,
): string {
  // Lazy tree: force-parse up to `to` so code/HR detection is reliable for selections beyond
  // the viewport (mirrors ligature-plugin.test.ts, which forces the full tree).
  ensureSyntaxTree(state, to, 5000);

  // Excluded ranges = InlineCode + FencedCode (shared helper) + HorizontalRule. HR is ALWAYS
  // excluded here — copy has no cursor, so the on-screen "reveal raw --- near the cursor"
  // affordance does not apply (see collectExcludedRanges ARCH note above).
  const excluded: CodeRange[] = collectCodeRanges(state, [{ from, to }]);
  syntaxTree(state).iterate({
    from,
    to,
    enter(node) {
      if (node.name === 'HorizontalRule') {
        excluded.push({ from: node.from, to: node.to });
      }
    },
  });
  const inExcluded = makeInCodeChecker(excluded);

  const text = state.doc.sliceString(from, to);
  let out = '';
  let i = 0;

  while (i < text.length) {
    const ch = text[i];

    if (!TRIGGER_CHARS.has(ch)) {
      out += ch;
      i++;
      continue;
    }

    // Skip escaped sequences — mirror buildLigatureDecorations (within-slice check only).
    if (i > 0 && text[i - 1] === '\\') {
      out += ch;
      i++;
      continue;
    }

    let matched = false;
    for (const rule of LIGATURE_RULES) {
      if (text.startsWith(rule.pattern, i)) {
        const absFrom = from + i;
        const absTo = absFrom + rule.pattern.length;
        out += inExcluded(absFrom, absTo) ? rule.pattern : rule.glyph;
        i += rule.pattern.length;
        matched = true;
        break;
      }
    }

    if (!matched) {
      out += ch;
      i++;
    }
  }

  return out;
}

// ─── ViewPlugin ─────────────────────────────────────────────────────────────

export const ligaturePlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    private lastViewport: readonly { from: number; to: number }[];

    constructor(view: EditorView) {
      this.decorations = buildLigatureDecorations(view);
      this.lastViewport = view.visibleRanges.map((r) => ({
        from: r.from,
        to: r.to,
      }));
    }

    update(update: ViewUpdate) {
      if (
        update.docChanged ||
        update.selectionSet ||
        syntaxTree(update.state) !== syntaxTree(update.startState)
      ) {
        this.decorations = buildLigatureDecorations(update.view);
        this.lastViewport = update.view.visibleRanges.map((r) => ({
          from: r.from,
          to: r.to,
        }));
        return;
      }
      if (
        update.viewportChanged &&
        !isViewportCovered(this.lastViewport, update.view.visibleRanges)
      ) {
        this.decorations = buildLigatureDecorations(update.view);
        this.lastViewport = update.view.visibleRanges.map((r) => ({
          from: r.from,
          to: r.to,
        }));
      }
    }
  },
  {
    decorations: (v) => v.decorations,
    eventHandlers: {
      // WHY: CM6 posAtCoords fails to resolve clicks on Decoration.replace widgets,
      // leaving selection unchanged. We intercept mousedown and set cursor explicitly.
      mousedown(event: MouseEvent, view: EditorView) {
        const target = event.target as HTMLElement;
        const lig = target.closest('.cm-ligature') as HTMLElement | null;
        if (!lig) return false;
        const pos = view.posAtDOM(lig);
        view.dispatch({ selection: { anchor: pos } });
        event.preventDefault();
        return true;
      },
    },
  },
);
