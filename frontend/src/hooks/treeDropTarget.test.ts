/** treeDropTarget — pure resolution of a tree drop into a visible-row gap.
 *
 * Pins the gap rule: the row's hovered half picks the gap; the pointer's depth
 * (X) picks the landing depth inside the gap's legal range; the dragged doc's
 * own subtree is never a valid destination.
 */

import { describe, it, expect } from 'vitest';
import { resolveTreeDrop, resolveTreeNest, TREE_INDENT, type TreeDropRow } from './treeDropTarget';

// Visible fixture (top→bottom): A expanded, A1 expanded, A2 COLLAPSED (its
// children are not rows), B a root sibling of A.
const rows: TreeDropRow[] = [
  { id: 'A', parentId: null, depth: 0 },
  { id: 'A1', parentId: 'A', depth: 1 },
  { id: 'A1a', parentId: 'A1', depth: 2 },
  { id: 'A2', parentId: 'A', depth: 1 },
  { id: 'B', parentId: null, depth: 0 },
];
const idx = (id: string) => rows.findIndex(r => r.id === id);

describe('resolveTreeDrop', () => {
  it('the top gap is root/top only — no depth choice', () => {
    expect(resolveTreeDrop(rows, 0, 3, 'B')).toEqual({ parentId: null, afterId: null, depth: 0 });
  });

  it('a gap inside an expanded parent, above its first child, nests as the FIRST child', () => {
    // Gap between A and A1: only depth 1 is legal and it means parent=A, after=null.
    expect(resolveTreeDrop(rows, idx('A1'), 1, 'B')).toEqual({ parentId: 'A', afterId: null, depth: 1 });
  });

  it('an end-of-subtree gap resolves per pointer depth: each X depth a different ancestor/after', () => {
    // Gap between A2 (collapsed, depth 1) and B (depth 0): legal d ∈ [0, 1].
    expect(resolveTreeDrop(rows, idx('B'), 0, 'B')).toEqual({ parentId: null, afterId: 'A', depth: 0 });
    expect(resolveTreeDrop(rows, idx('B'), 1, 'B')).toEqual({ parentId: 'A', afterId: 'A2', depth: 1 });
    // Out-of-range depths clamp into the gap's range (never past A2's level).
    expect(resolveTreeDrop(rows, idx('B'), 7, 'B')).toEqual({ parentId: 'A', afterId: 'A2', depth: 1 });
    expect(resolveTreeDrop(rows, idx('B'), -3, 'B')).toEqual({ parentId: null, afterId: 'A', depth: 0 });
  });

  it('a gap after a deeper expanded subtree offers each level back up to the deeper side', () => {
    // Gap between A1a (depth 2) and A2 (depth 1): legal d ∈ [1, 2].
    expect(resolveTreeDrop(rows, idx('A2'), 2, 'B')).toEqual({ parentId: 'A1', afterId: 'A1a', depth: 2 });
    expect(resolveTreeDrop(rows, idx('A2'), 1, 'B')).toEqual({ parentId: 'A', afterId: 'A1', depth: 1 });
  });

  it('the bottom gap (no row below) counts the missing row as depth 0', () => {
    const two: TreeDropRow[] = [
      { id: 'A', parentId: null, depth: 0 },
      { id: 'A1', parentId: 'A', depth: 1 },
    ];
    // Dragging A1: at depth 0 it re-roots right after A; at depth 1 the landing
    // spot is its own current slot (after itself) — refused.
    expect(resolveTreeDrop(two, 2, 0, 'A1')).toEqual({ parentId: null, afterId: 'A', depth: 0 });
    expect(resolveTreeDrop(two, 2, 1, 'A1')).toBeUndefined();
  });

  it('gaps inside the dragged subtree and drops onto it resolve to undefined', () => {
    // Dragging A: its subtree is A1, A1a, A2 (contiguous deeper rows after it).
    // Gap between A1 and A1a would nest under A1 (inside the subtree).
    expect(resolveTreeDrop(rows, idx('A1a'), 2, 'A')).toBeUndefined();
    // Gap between A1a and A2 at both legal depths lands inside the subtree.
    expect(resolveTreeDrop(rows, idx('A2'), 2, 'A')).toBeUndefined();
    expect(resolveTreeDrop(rows, idx('A2'), 1, 'A')).toBeUndefined();
    // End-of-subtree gap: every depth lands on/inside the dragged doc — refused
    // (dropping a doc right after its own subtree is the no-op it already sits in).
    expect(resolveTreeDrop(rows, idx('B'), 1, 'A')).toBeUndefined();
    expect(resolveTreeDrop(rows, idx('B'), 0, 'A')).toBeUndefined();
    // …but the gap AFTER B is a valid new spot for the whole subtree.
    expect(resolveTreeDrop(rows, rows.length, 0, 'A')).toEqual({ parentId: null, afterId: 'B', depth: 0 });
  });

  it('a dragged id that is not a visible row resolves to undefined', () => {
    expect(resolveTreeDrop(rows, 2, 1, 'nope')).toBeUndefined();
  });

  it('exports the shared per-level indent', () => {
    expect(TREE_INDENT).toBe(12);
  });
});

describe('resolveTreeNest', () => {
  it('drops INTO any row as its FIRST child — expanded, collapsed or leaf', () => {
    expect(resolveTreeNest(rows, idx('A1'), 'B')).toEqual({ parentId: 'A1', afterId: null, depth: 2 });
    expect(resolveTreeNest(rows, idx('A2'), 'B')).toEqual({ parentId: 'A2', afterId: null, depth: 2 });
    expect(resolveTreeNest(rows, idx('A1a'), 'B')).toEqual({ parentId: 'A1a', afterId: null, depth: 3 });
  });

  it('the dragged doc and its visible descendants are never a nest target', () => {
    expect(resolveTreeNest(rows, idx('A'), 'A')).toBeUndefined();
    expect(resolveTreeNest(rows, idx('A1'), 'A')).toBeUndefined();
    expect(resolveTreeNest(rows, idx('A1a'), 'A')).toBeUndefined();
    // A sibling outside the subtree is fine.
    expect(resolveTreeNest(rows, idx('B'), 'A')).toEqual({ parentId: 'B', afterId: null, depth: 1 });
  });

  it('a dragged id that is not a visible row resolves to undefined', () => {
    expect(resolveTreeNest(rows, idx('A'), 'nope')).toBeUndefined();
  });
});
