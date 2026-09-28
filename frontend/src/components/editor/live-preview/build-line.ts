/**
 * Build ALL line decorations (ViewPlugin — visible ranges only).
 *
 * Produces line-level Decoration.line for blockquotes, HR, code block backgrounds,
 * and list hanging indents. No widgets or string operations — trivially cheap.
 *
 * ARCH: Must be a ViewPlugin (viewport-only), NOT a StateField. Line decorations
 * that add CSS padding (cm-fenced-code paddingLeft/Right, cm-quote borderLeft+
 * paddingLeft, cm-list-line inline padding-left) reduce the effective text wrap
 * width. CM6's HeightOracle estimates off-screen line heights using full
 * contentWidth and does NOT account for per-line decoration padding. If these
 * decorations are applied to off-screen lines via StateField, the oracle
 * underestimates their height → contentHeight model collapses when measured
 * heights are discarded → scrollTop exceeds model → visible scroll jump.
 * ViewPlugin limits decorations to visible lines, so off-screen lines keep
 * their full-width estimate (which is correct since no padding is applied).
 *
 * Split into two builders for performance:
 * - buildListLineDecorations: cursor-independent, rebuilds on doc/tree only
 * - buildCursorLineDecorations: cursor-dependent (blockquote, HR, fenced code),
 *   rebuilds on doc/tree/selection
 */
import { syntaxTree } from '@codemirror/language';
import { Decoration, type DecorationSet, type EditorView } from '@codemirror/view';
import { RangeSetBuilder } from '@codemirror/state';
import { getListNestingLevel, getListItemContentLines, listIndentStyle } from './list-indent';
import { revealAtCursor } from './effects';

export function buildListLineDecorations(view: EditorView): DecorationSet {
  const state = view.state;
  const lineDecs: { from: number; cls: string; attributes?: Record<string, string> }[] = [];

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter(node) {
        if (node.name !== 'ListItem') return;

        const nestingLevel = getListNestingLevel(node.node);
        const lines = getListItemContentLines(node, state);
        for (const { line, leadingSpaces, isFirst } of lines) {
          if (line.from < from || line.from > to) continue;
          if (isFirst || leadingSpaces >= lines.contentCol) {
            lineDecs.push({
              from: line.from,
              cls: 'cm-list-line',
              attributes: { style: listIndentStyle(nestingLevel) },
            });
          }
        }
      },
    });
  }

  lineDecs.sort((a, b) => a.from - b.from);

  const builder = new RangeSetBuilder<Decoration>();
  for (const { from, cls, attributes } of lineDecs) {
    builder.add(from, from, Decoration.line({ class: cls, ...(attributes && { attributes }) }));
  }
  return builder.finish();
}

export function buildCursorLineDecorations(view: EditorView): DecorationSet {
  const state = view.state;
  const selRanges = state.facet(revealAtCursor) ? state.selection.ranges : [];
  const isCursorIn = (from: number, to: number) =>
    selRanges.some((r) => r.from <= to && r.to >= from);

  const lineDecs: { from: number; cls: string }[] = [];

  for (const { from, to } of view.visibleRanges) {
    syntaxTree(state).iterate({
      from,
      to,
      enter(node) {
        if (node.name === 'Blockquote') {
          if (!isCursorIn(node.from, node.to)) {
            for (let pos = node.from; pos < node.to; ) {
              const line = state.doc.lineAt(pos);
              lineDecs.push({ from: line.from, cls: 'cm-quote' });
              pos = line.to + 1;
            }
          }
        }

        if (node.name === 'HorizontalRule') {
          if (!isCursorIn(node.from, node.to)) {
            lineDecs.push({ from: state.doc.lineAt(node.from).from, cls: 'cm-hr-line' });
          }
          return false;
        }

        if (node.name === 'FencedCode') {
          const cursorInside = isCursorIn(node.from, node.to);

          const cursor = node.node.cursor();
          let openFenceLineFrom = -1;
          let closeFenceLineFrom = -1;

          if (cursor.firstChild()) {
            do {
              if (cursor.name === 'CodeMark') {
                const lineFrom = state.doc.lineAt(cursor.from).from;
                if (openFenceLineFrom === -1) openFenceLineFrom = lineFrom;
                else closeFenceLineFrom = lineFrom;
              } else if (cursor.name === 'CodeText') {
                const textFrom = cursor.from;
                const textTo = cursor.to;
                if (textFrom >= textTo) continue;

                const visFrom = Math.max(textFrom, from);
                const visTo = Math.min(textTo, to);
                if (visFrom <= visTo) {
                  const firstLine = state.doc.lineAt(visFrom);
                  const lastLine = state.doc.lineAt(visTo);
                  for (let n = firstLine.number; n <= lastLine.number; n++) {
                    const lineFrom2 = state.doc.line(n).from;
                    const classes = ['cm-fenced-code'];
                    if (lineFrom2 === state.doc.lineAt(textFrom).from) classes.push('cm-fenced-code-first');
                    if (lineFrom2 === state.doc.lineAt(textTo).from) classes.push('cm-fenced-code-last');
                    lineDecs.push({ from: lineFrom2, cls: classes.join(' ') });
                  }
                }
              }
            } while (cursor.nextSibling());
          }

          const editMod = cursorInside ? ' cm-fenced-code-fence--editing' : '';
          if (openFenceLineFrom !== -1) {
            lineDecs.push({ from: openFenceLineFrom, cls: 'cm-fenced-code cm-fenced-code-fence cm-fenced-code-fence-first' + editMod });
          }
          if (closeFenceLineFrom !== -1) {
            lineDecs.push({ from: closeFenceLineFrom, cls: 'cm-fenced-code cm-fenced-code-fence cm-fenced-code-fence-last' + editMod });
          }

          return false;
        }
      },
    });
  }

  lineDecs.sort((a, b) => a.from - b.from);

  const builder = new RangeSetBuilder<Decoration>();
  for (const { from, cls } of lineDecs) {
    builder.add(from, from, Decoration.line({ class: cls }));
  }
  return builder.finish();
}
