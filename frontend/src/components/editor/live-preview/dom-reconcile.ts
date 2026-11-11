/** Minimal-move DOM child reconciliation for the id-keyed table widget. */

export function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a === b || (a.length === b.length && a.every((id, i) => id === b[i]));
}

/**
 * Indices of `seq` forming its longest strictly increasing run, ignoring entries < 0
 * (nodes not yet in the parent). The nodes at these indices are already in order and
 * stay where they are; only the rest move.
 */
function stableIndices(seq: readonly number[]): Set<number> {
  const tails: number[] = [];
  const prev = new Array<number>(seq.length).fill(-1);
  seq.forEach((v, i) => {
    if (v < 0) return;
    let lo = 0;
    let hi = tails.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (seq[tails[mid]] < v) lo = mid + 1; else hi = mid;
    }
    if (lo > 0) prev[i] = tails[lo - 1];
    tails[lo] = i;
  });
  const out = new Set<number>();
  for (let i = tails.at(-1) ?? -1; i >= 0; i = prev[i]) out.add(i);
  return out;
}

/**
 * Make `parent`'s children exactly `nodes`, in order, moving only the nodes outside the
 * longest run already in order — one moved row is one DOM move, and every other node
 * (with the cell editors inside it) stays attached. Nodes leaving the parent must be
 * removed by the caller first.
 * WHY minimal moves: moving a node that contains the focused cell editor drops its focus.
 */
export function reconcileChildren(parent: Element, nodes: readonly Element[]): void {
  const at = new Map<Element, number>();
  Array.from(parent.children).forEach((n, i) => at.set(n, i));
  const stable = stableIndices(nodes.map((n) => at.get(n) ?? -1));
  let next: Element | null = null;
  for (let i = nodes.length - 1; i >= 0; i--) {
    if (!stable.has(i)) parent.insertBefore(nodes[i], next);
    next = nodes[i];
  }
}
