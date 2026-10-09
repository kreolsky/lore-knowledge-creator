/**
 * treeDropTarget — the tree half's drop RESOLUTION (pure, DOM-free).
 *
 * The visible tree is a flat row list; a drop lands in a GAP between two
 * consecutive rows, and the pointer's X picks the target depth (VS Code /
 * Notion style); the row's middle drops INTO it instead (resolveTreeNest).
 * resolveTreeDrop turns (rows, gap, pointer depth) into the
 * (parent_id, after_id) commit payload plus the depth the drop line indents
 * to; useSiblingDragReorder's treeDragAdapter reads the rows off the DOM,
 * maps pointer X → depth and draws the line.
 */

/** One visible tree row, top→bottom. parentId null = project root. */
export interface TreeDropRow {
  id: string;
  parentId: string | null;
  depth: number;
}

/** A resolved tree drop: the reorder/move payload + the line's indent depth. */
export interface TreeDrop {
  parentId: string | null;
  afterId: string | null;
  depth: number;
}

/** Per-level row indent (px) — shared by the row renderer's paddingLeft and the
 * pointer-X→depth math so the drop line always sits at a real indent. */
export const TREE_INDENT = 12;

/** The dragged subtree: the doc itself + the contiguous deeper rows right after
 * it (its visible descendants). undefined = the dragged id is not a visible row. */
function draggedSubtree(rows: TreeDropRow[], draggedId: string): Set<string> | undefined {
  const dragIdx = rows.findIndex(r => r.id === draggedId);
  if (dragIdx === -1) return undefined;
  const subtree = new Set([draggedId]);
  for (let i = dragIdx + 1; i < rows.length && rows[i].depth > rows[dragIdx].depth; i++) {
    subtree.add(rows[i].id);
  }
  return subtree;
}

/**
 * Resolve the drop INTO rows[rowIndex] (the pointer over the row's middle):
 * the dragged doc becomes its FIRST child, whether the row is expanded,
 * collapsed or a leaf. Same subtree rule as resolveTreeDrop — the dragged doc
 * and its visible descendants are never a nest target.
 *
 * WHY: first, not last — a re-parent lands at the TOP of the new sibling group
 * (newest-first), the same as the parent picker (backend/documents/update.py
 * _apply_parent_update), so both gestures give one result.
 */
export function resolveTreeNest(
  rows: TreeDropRow[],
  rowIndex: number,
  draggedId: string,
): TreeDrop | undefined {
  const subtree = draggedSubtree(rows, draggedId);
  const row = rows[rowIndex];
  if (!subtree || !row || subtree.has(row.id)) return undefined;
  return { parentId: row.id, afterId: null, depth: row.depth + 1 };
}

/**
 * Resolve the drop into the gap ABOVE rows[gapIndex] (0 = above the first row,
 * rows.length = below the last one) at `pointerDepth` (the depth the pointer's
 * X maps to, UNclamped — clamping to the gap's legal range happens here).
 *
 * Gap rule: A = row above, B = row below; a missing B counts depth 0; a
 * missing A is the top gap (root, after null — no depth choice: nothing above
 * to nest into). Legal depth d ∈ [depthB, max(depthA, depthB)]; d = depthA + 1
 * (reachable only when A is expanded, i.e. B is A's first child) nests as A's
 * FIRST child; any other d lands AFTER A's ancestor at depth d (A itself when
 * d = depthA), under that row's parent.
 *
 * INVARIANT: a drop never re-parents a doc into its own subtree nor lands
 * inside it — those gaps resolve to undefined (no line, no commit). Why: both
 * would cycle or reduce to a no-op; showing a line for them would promise a
 * move that cannot happen.
 */
export function resolveTreeDrop(
  rows: TreeDropRow[],
  gapIndex: number,
  pointerDepth: number,
  draggedId: string,
): TreeDrop | undefined {
  const subtree = draggedSubtree(rows, draggedId);
  if (!subtree) return undefined;

  const above = gapIndex > 0 ? rows[gapIndex - 1] : null;
  const below = gapIndex < rows.length ? rows[gapIndex] : null;
  if (!above) return { parentId: null, afterId: null, depth: 0 };

  const dA = above.depth;
  const dB = below ? below.depth : 0;
  const d = Math.min(Math.max(pointerDepth, dB), Math.max(dA, dB));

  if (d === dA + 1) {
    if (subtree.has(above.id)) return undefined;
    return { parentId: above.id, afterId: null, depth: d };
  }
  // Walk up from `above` (itself included) to its ancestor at depth d — that
  // row is the landing `after` sibling; the new parent is that row's parent.
  // Ancestors of a visible row are visible (expanded), so the chain always
  // resolves inside `rows`.
  const byId = new Map(rows.map(r => [r.id, r]));
  let after: TreeDropRow | undefined = above;
  while (after && after.depth > d) {
    after = after.parentId == null ? undefined : byId.get(after.parentId);
  }
  if (!after) return undefined;
  const parentId = after.parentId ?? null;
  if ((parentId !== null && subtree.has(parentId)) || subtree.has(after.id)) return undefined;
  return { parentId, afterId: after.id, depth: d };
}
