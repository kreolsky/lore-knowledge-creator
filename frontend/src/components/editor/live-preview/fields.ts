/**
 * StateField and ViewPlugin definitions for live-preview.
 *
 * WHY: livePreviewField (StateField) for structural decorations (replace, mark, widget) —
 *   iterates full syntax tree, recalculates on doc/selection/tree changes, ZERO cost on scroll.
 * WHY: lineDecorField (StateField) for ALL line decorations (code block bg, blockquote, HR,
 *   list hanging indent) — full document, no viewport dependency. Previously a ViewPlugin
 *   (viewport-only) which caused contentHeight instability from padding-induced wrapping.
 * WHY: tableRenderField (StateField) for multi-line Decoration.replace — CM6 line-break restriction.
 * WHY: mathBlockRenderField (StateField) for block math ($$...$$) — same line-break restriction.
 * WHY: transcludeMap module-level Map — bridge between React state and CM6 plugin context.
 * SYSTEM: live-preview — CM6 plugin for inline markdown rendering (syntax hiding, checkboxes, images, tables, math)
 */
import { syntaxTree } from '@codemirror/language';
import type { SyntaxNode } from '@lezer/common';
import {
  Decoration,
  DecorationSet,
  EditorView,
  ViewPlugin,
  ViewUpdate,
} from '@codemirror/view';
import { EditorState, Range, RangeSetBuilder, StateField } from '@codemirror/state';
import { findSingleLineDisplayMath, findBlockMath } from '../math-render';
import { linkContextChanged, revealAtCursor } from './effects';
import { BlockMathWidget, CheckboxWidget, MermaidWidget } from './widgets';
import { TableWidget } from './table-widget';
import { TableBlockWidget } from './table-block-widget';
import { parseTarget, extractEmbedFromImageNode } from './transclusion-grammar';
import { buildStructuralDecorations } from './build-structural';
import { buildListLineDecorations, buildCursorLineDecorations } from './build-line';
import { getListNestingLevel, getListIndent } from './list-indent';
import { blockReplaceField, type BlockReplaceState } from './block-replace-field';

// ─── Structural StateField ────────────────────────────────────────────────────

// WHY: Filter the structural decoration set down to just CheckboxWidget
// replace ranges, then surface them via EditorView.atomicRanges. Cursor
// commands (cursorLeft, moveByChar, Home/End) jump over the widget instead
// of landing inside its replaced range. Implicit Decoration.replace
// atomicity is browser-inconsistent at line-end boundaries; the explicit
// facet makes the contract uniform.
function checkboxAtomicRanges(decs: DecorationSet): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  decs.between(0, Number.MAX_SAFE_INTEGER, (from, to, dec) => {
    const spec = (dec as unknown as { spec: { widget?: unknown } }).spec;
    if (spec.widget instanceof CheckboxWidget) builder.add(from, to, dec);
  });
  return builder.finish();
}

export const livePreviewField = StateField.define<DecorationSet>({
  create(state) { return buildStructuralDecorations(state); },
  update(decs, tr) {
    if (tr.docChanged || tr.selection
        || tr.effects.some(e => e.is(linkContextChanged))
        || syntaxTree(tr.state) !== syntaxTree(tr.startState)) {
      return buildStructuralDecorations(tr.state);
    }
    return decs;
  },
  provide: (f) => [
    EditorView.decorations.from(f),
    EditorView.atomicRanges.of(view => checkboxAtomicRanges(view.state.field(f))),
  ],
});

// ─── Line Decorations ViewPlugins ──────────────────────────────────────────────
// WHY: Line decorations MUST use ViewPlugin (viewport-only), not StateField.
// Line decorations add CSS padding (cm-fenced-code padding, cm-quote borderLeft+
// paddingLeft, cm-list-line inline padding-left) that reduces effective text wrap
// width. CM6's HeightOracle estimates off-screen line heights using full
// contentWidth, ignoring per-line decoration padding. A StateField applies
// decorations to ALL lines, causing the oracle to underestimate off-screen line
// heights → contentHeight model collapses → scroll jump. ViewPlugin limits
// decorations to visible lines, keeping the oracle's off-screen estimate correct.
//
// Split into two ViewPlugins for performance: list indent is cursor-independent
// (no selection trigger), blockquote/HR/fenced code are cursor-dependent.

export const listLinePlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = buildListLineDecorations(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged
          || syntaxTree(update.state) !== syntaxTree(update.startState)) {
        this.decorations = buildListLineDecorations(update.view);
      }
    }
  },
  { decorations: (v) => v.decorations },
);

export const cursorLinePlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = buildCursorLineDecorations(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.selectionSet || update.viewportChanged
          || syntaxTree(update.state) !== syntaxTree(update.startState)) {
        this.decorations = buildCursorLineDecorations(update.view);
      }
    }
  },
  { decorations: (v) => v.decorations },
);

// ─── Table rendering (StateField) ─────────────────────────────────────────────

function buildTableDecorationsWithBounds(state: EditorState): BlockReplaceState {
  const selRanges = state.facet(revealAtCursor) ? state.selection.ranges : [];
  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  const ranges: Range<Decoration>[] = [];
  const bounds: { from: number; to: number }[] = [];

  syntaxTree(state).iterate({
    enter(node) {
      if (node.name !== 'Table') return;
      // INVARIANT(corruption): the block replace must span whole lines — from the start of the
      // first row's line to the end of the last row's line — not the raw Lezer node.
      // Why: GFM allows up to 3 leading spaces, so the Table node can begin AFTER a
      // leading space. Replacing only node.from..node.to leaves that space as an
      // orphan 1-char `.cm-line`; CM6 measureTextSize() samples it (<=20 chars, pure
      // ASCII) → charWidth collapses to space-width (~4px vs ~7px) → contentHeight
      // craters below the real DOM → scrollTop clamp = multi-thousand-px scroll jump.
      // Reproduced 2026-06-01 (leading space before table). See lessons/2026-05-30.
      const blockFrom = state.doc.lineAt(node.from).from;
      const blockTo = state.doc.lineAt(node.to).to;
      bounds.push({ from: blockFrom, to: blockTo });
      if (isCursorIn(blockFrom, blockTo)) return false;
      const rawText = state.doc.sliceString(node.from, node.to);
      ranges.push(
        Decoration.replace({ widget: new TableWidget(rawText), block: true })
          .range(blockFrom, blockTo),
      );
      return false;
    },
  });

  return { decs: Decoration.set(ranges, true), bounds };
}

/** Table block replacement — separate from livePreviewPlugin (ViewPlugin). */
/** Table block replacement — separate from livePreviewPlugin (ViewPlugin). */
export const tableRenderField = blockReplaceField(buildTableDecorationsWithBounds, {
  cursorSensitive: true,
});

// ─── Table block object rendering (StateField) ────────────────────────────────
// WHY: a `![label](table:id)` transclusion anchor is replaced by a block widget that
// renders the doc-local `tables` Yjs subtree. Same multi-line Decoration.replace pattern
// as tableRenderField (CM6 line-break restriction → StateField, never ViewPlugin). The
// block range spans whole lines so a leading space cannot orphan a 1-char `.cm-line`
// (charWidth-collapse scroll-jump, see tableRenderField).

// INVARIANT: the table block widget is ALWAYS rendered (no reveal-raw-on-cursor like
// tableRenderField/GFM). Why: its cells are `contenteditable` — editing happens IN the
// widget, so collapsing it to raw text on cursor would make editing impossible. The raw
// `![label](table:id)` anchor is never shown; deletion goes through the widget controls.
// The same invariant is why this field is ATOMIC (`atomic: true`): the hidden anchor text
// must never receive a caret or a partial edit — motion skips it as one unit and a
// Backspace that reaches it removes the whole anchor, never a mangled fragment that
// orphans the table behind the error plate (same contract CheckboxWidget registers via
// livePreviewField). Why not the cursorSensitive siblings: they reveal raw markdown
// under the caret, so their ranges must stay enterable.
function buildTableBlockDecorations(state: EditorState): DecorationSet {
  const ranges: Range<Decoration>[] = [];

  syntaxTree(state).iterate({
    enter(node) {
      if (node.name !== 'Image') return;
      const embed = extractEmbedFromImageNode(node.node, state);
      if (!embed) return;
      const parsed = parseTarget(embed.urlText);
      if (!parsed || parsed.scheme !== 'table') return;

      const blockFrom = state.doc.lineAt(node.from).from;
      const blockTo = state.doc.lineAt(node.to).to;
      ranges.push(
        Decoration.replace({
          widget: new TableBlockWidget(parsed.id, embed.alt),
          block: true,
        }).range(blockFrom, blockTo),
      );
      return false;
    },
  });

  return Decoration.set(ranges, true);
}

export const tableBlockField = blockReplaceField(
  (state) => ({ decs: buildTableBlockDecorations(state), bounds: [] }),
  { cursorSensitive: false, atomic: true },
);

// ─── Math block rendering (StateField) ───────────────────────────────────────
// WHY: Block math ($$...$$) spans multiple lines → must use StateField for
// Decoration.replace (CM6 line-break restriction), same pattern as tableRenderField.

function getListIndentAtPos(state: EditorState, pos: number): string {
  let node = syntaxTree(state).resolveInner(pos, 1) as SyntaxNode | null;
  while (node) {
    if (node.name === 'ListItem') {
      const indent = getListIndent(getListNestingLevel(node));
      return indent > 0 ? `${indent}em` : '0';
    }
    node = node.parent;
  }
  return '0';
}

function buildMathBlockDecorations(state: EditorState): BlockReplaceState {
  const selRanges = state.facet(revealAtCursor) ? state.selection.ranges : [];
  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  const fencedRanges: { from: number; to: number }[] = [];
  const tableRanges: { from: number; to: number }[] = [];
  syntaxTree(state).iterate({
    enter(node) {
      if (node.name === 'FencedCode') fencedRanges.push({ from: node.from, to: node.to });
      if (node.name === 'Table') tableRanges.push({ from: node.from, to: node.to });
    },
  });
  const inFenced = (from: number, to: number) =>
    fencedRanges.some((r) => from < r.to && to > r.from);
  const inTable = (from: number, to: number) =>
    tableRanges.some((r) => from >= r.from && to <= r.to);

  const multiLine = findBlockMath(state.doc);
  const singleLine: { from: number; to: number; latex: string }[] = [];
  for (let i = 1; i <= state.doc.lines; i++) {
    const line = state.doc.line(i);
    for (const m of findSingleLineDisplayMath(line.text, line.from)) {
      if (multiLine.some((b) => m.from >= b.from && m.to <= b.to)) continue;
      const before = state.doc.sliceString(line.from, m.from);
      const after = state.doc.sliceString(m.to, line.to);
      if (/^\s*$/.test(before) && /^\s*$/.test(after)) {
        singleLine.push({ from: line.from, to: line.to, latex: m.latex });
      } else {
        singleLine.push(m);
      }
    }
  }

  const allBlocks = [...multiLine, ...singleLine];
  const ranges: Range<Decoration>[] = [];
  const bounds: { from: number; to: number }[] = [];

  for (const block of allBlocks) {
    if (inFenced(block.from, block.to)) continue;
    if (inTable(block.from, block.to)) continue;
    bounds.push({ from: block.from, to: block.to });
    if (isCursorIn(block.from, block.to)) continue;
    const indent = getListIndentAtPos(state, block.from);
    ranges.push(
      Decoration.replace({ widget: new BlockMathWidget(block.latex, indent), block: true })
        .range(block.from, block.to),
    );
  }

  return { decs: Decoration.set(ranges, true), bounds };
}

export const mathBlockRenderField = blockReplaceField(buildMathBlockDecorations, {
  cursorSensitive: true,
});

// ─── Mermaid block rendering (StateField) ───────────────────────────────────
// WHY: Mermaid code blocks span multiple lines → StateField with Decoration.replace
// (CM6 line-break restriction), same pattern as mathBlockRenderField.

function buildMermaidBlockDecorations(state: EditorState): BlockReplaceState {
  const selRanges = state.facet(revealAtCursor) ? state.selection.ranges : [];
  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  const ranges: Range<Decoration>[] = [];
  const bounds: { from: number; to: number }[] = [];

  syntaxTree(state).iterate({
    enter(node) {
      if (node.name !== 'FencedCode') return;

      const cursor = node.node.cursor();
      let language = '';
      let codeText = '';

      if (cursor.firstChild()) {
        do {
          if (cursor.name === 'CodeInfo') {
            language = state.doc.sliceString(cursor.from, cursor.to).trim().toLowerCase();
          } else if (cursor.name === 'CodeText') {
            codeText = state.doc.sliceString(cursor.from, cursor.to);
          }
        } while (cursor.nextSibling());
      }

      if (language !== 'mermaid') return;

      bounds.push({ from: node.from, to: node.to });
      if (isCursorIn(node.from, node.to)) return;

      ranges.push(
        Decoration.replace({ widget: new MermaidWidget(codeText), block: true })
          .range(node.from, node.to),
      );
    },
  });

  return { decs: Decoration.set(ranges, true), bounds };
}

export const mermaidBlockRenderField = blockReplaceField(buildMermaidBlockDecorations, {
  cursorSensitive: true,
});
