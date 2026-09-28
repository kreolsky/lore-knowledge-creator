/** Shared helpers for detecting inline-code / fenced-code ranges in CM6 syntax tree. */
// SYSTEM: code-range-utils — shared helpers for detecting code ranges (InlineCode, FencedCode)

import { syntaxTree } from '@codemirror/language';
import { EditorState } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';

export interface CodeRange { from: number; to: number }

/**
 * Collect InlineCode + FencedCode ranges from the syntax tree.
 * Pass `viewportRanges` (e.g. `view.visibleRanges`) to limit traversal — recommended
 * for ViewPlugin decorations that already work per-viewport. Omit to scan the whole doc.
 */
export function collectCodeRanges(
  state: EditorState,
  viewportRanges?: readonly { from: number; to: number }[],
): CodeRange[] {
  const out: CodeRange[] = [];
  const tree = syntaxTree(state);
  const enter = (node: { name: string; from: number; to: number }) => {
    if (node.name === 'InlineCode') {
      out.push({ from: node.from, to: node.to });
      return;
    }
    if (node.name === 'FencedCode') {
      out.push({ from: node.from, to: node.to });
      return false;
    }
  };
  if (viewportRanges?.length) {
    for (const r of viewportRanges) tree.iterate({ from: r.from, to: r.to, enter });
  } else {
    tree.iterate({ enter });
  }
  return out;
}

/** Single-position membership test — used by event handlers (e.g. link click). */
export function isPosInCode(state: EditorState, pos: number): boolean {
  for (let cur: SyntaxNode | null = syntaxTree(state).resolveInner(pos, 1); cur; cur = cur.parent) {
    if (cur.name === 'InlineCode' || cur.name === 'FencedCode') return true;
  }
  return false;
}

/** Build an O(n) overlap checker over a pre-collected ranges list. */
export function makeInCodeChecker(ranges: CodeRange[]): (from: number, to: number) => boolean {
  return (from: number, to: number) =>
    ranges.some((r) => from < r.to && to > r.from);
}
