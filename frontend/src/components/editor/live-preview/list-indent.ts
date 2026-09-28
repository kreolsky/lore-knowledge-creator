/**
 * Shared list-indent constants and helpers.
 *
 * ARCH: STEP (per-level gap) + GUTTER (marker area) in em units.
 * Formula: padding-left = (nestingLevel-1)*STEP + GUTTER (text/wrap column),
 * text-indent = -(GUTTER - BASE_PADDING) — a CONSTANT pull-back so the bullet hangs
 * at (nestingLevel-1)*STEP + BASE_PADDING, i.e. it steps right per nesting level.
 * Used by build-line.ts (inline styles) and fields.ts (math widget padding).
 *
 * INVARIANT: text-indent must NOT depend on nestingLevel. A per-level text-indent of
 * -(getListIndent - BASE_PADDING) cancels the growing padding and collapses every
 * level's first line (the bullet) to BASE_PADDING → all nested bullets align → flat.  Why: a per-level text-indent cancels the growing padding-left and collapses every bullet to BASE_PADDING (flat); text-indent must stay constant.
 * Why: space-indented lists hide their leading whitespace, so nesting comes only from
 * this style; the collapsed bullet made nested space-lists render flat (user-reported).
 */
import type { SyntaxNode, SyntaxNodeRef } from '@lezer/common';
import type { EditorState } from '@codemirror/state';
import type { Line } from '@codemirror/state';
export const LIST_STEP = 1;
export const LIST_GUTTER = 1.5;
export const BASE_PADDING = 0.5;

export function getListNestingLevel(node: { parent: { name: string; parent: unknown } | null }): number {
  let level = 1;
  let ancestor = node.parent;
  while (ancestor) {
    if ((ancestor as { name: string }).name === 'ListItem') level++;
    ancestor = (ancestor as { parent: unknown }).parent as { name: string; parent: unknown } | null;
  }
  return level;
}

export function getListIndent(nestingLevel: number): number {
  return (nestingLevel - 1) * LIST_STEP + LIST_GUTTER;
}

export function listIndentStyle(nestingLevel: number): string {
  const indent = getListIndent(nestingLevel);
  // text-indent is CONSTANT (independent of nestingLevel) — see INVARIANT above.
  return `padding-left:${indent}em;text-indent:${-(LIST_GUTTER - BASE_PADDING)}em`;
}

/**
 * Returns the column offset where text content starts on the first line
 * of a ListItem (after ListMark + spaces, or TaskMarker + space).
 * Used to detect lazy continuation lines (lines with fewer leading spaces).
 */
export function getListItemContentColumn(node: { node: SyntaxNode; from: number }, state: EditorState): number {
  const listMark = node.node.getChildren('ListMark')[0];
  if (!listMark) return 0;

  const firstLine = state.doc.lineAt(node.from);

  let contentStart = listMark.to;
  const lineEnd = firstLine.to;
  while (contentStart < lineEnd && /[ \t]/.test(state.doc.sliceString(contentStart, contentStart + 1))) {
    contentStart++;
  }

  const taskMarker = node.node.getChildren('TaskMarker')[0];
  if (taskMarker) {
    let pos = taskMarker.to;
    if (state.doc.sliceString(pos, pos + 1) === ' ') pos++;
    contentStart = Math.max(contentStart, pos);
  }

  return contentStart - firstLine.from;
}

/**
 * Iterates the content lines of a ListItem (up to the first nested list child),
 * computing leading-space count for lazy-continuation detection.
 * Shared by build-line.ts (cm-list-line decorations) and build-structural.ts
 * (leading-space hiding) to prevent drift between the two.
 */
export interface ListItemContentLine {
  line: Line;
  leadingSpaces: number;
  isFirst: boolean;
}

export interface ListItemContentResult extends Array<ListItemContentLine> {
  contentCol: number;
}

export function getListItemContentLines(
  node: SyntaxNodeRef,
  state: EditorState,
): ListItemContentResult {
  const contentCol = getListItemContentColumn(node, state);

  let contentEnd = node.to;
  const nestedLists = [
    ...node.node.getChildren('BulletList'),
    ...node.node.getChildren('OrderedList'),
  ];
  for (const nl of nestedLists) {
    const nlLineStart = state.doc.lineAt(nl.from).from;
    if (nlLineStart < contentEnd) contentEnd = nlLineStart;
  }

  const result = [] as unknown as ListItemContentResult;
  result.contentCol = contentCol;

  let isFirst = true;
  for (let pos = node.from; pos < contentEnd; ) {
    const line = state.doc.lineAt(pos);
    const text = line.text;
    // Count leading spaces AND tabs: tab-indented lists must hide their indent too,
    // otherwise the rendered tab stacks on top of the per-level padding and double-indents.
    let leadingSpaces = 0;
    while (leadingSpaces < text.length && (text[leadingSpaces] === ' ' || text[leadingSpaces] === '\t')) {
      leadingSpaces++;
    }
    result.push({ line, leadingSpaces, isFirst });
    isFirst = false;
    pos = line.to + 1;
  }

  return result;
}
