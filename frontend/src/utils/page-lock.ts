/** Page PIN lock (SYSTEM: page-lock) — the one "is the open page still behind its PIN" projection. */
import type { AppState } from '../store/app-store';
import type { Document, DocumentTreeNode } from '../types';
import { buildParentMap } from './document-sort';

type LockState = Pick<AppState, 'currentDocument' | 'currentUser' | 'unlockedDocIds' | 'documents'>;

/** Gate value while the project's document list (the parent chain) has not arrived yet. */
export const LOCK_PENDING = ':pending';

// WHY a leaf module, not app-store: its readers' tests mock app-store wholesale, and a
// selector exported from the mocked module would be missing there.
// INVARIANT(security): the open page is gated by the OUTERMOST locked, not-PIN-opened page on its parent chain.
// Why: operator request — a child of a locked page reopened by URL or reload must show
// the locked ancestor's PIN screen, not its own text. The chain includes the page itself;
// while it is unknown (a reload commits the document one RTT before the list) the gate is
// PENDING, never open. Users with no locks walk nothing.
export function selectOpenPageLockedBy(s: LockState): string | null {
  const id = s.currentDocument?.document_id;
  const locked = s.currentUser?.locked_doc_ids;
  if (!id || !locked || locked.length === 0) return null;
  if (!s.documents.some(d => d.document_id === id)) return LOCK_PENDING;
  const parents = buildParentMap(s.documents);
  const seen = new Set<string>();
  let outermost: string | null = null;
  let cur: string | null = id;
  while (cur && !seen.has(cur)) {
    if (locked.includes(cur) && !s.unlockedDocIds.includes(cur)) outermost = cur;
    seen.add(cur);
    cur = parents.get(cur) ?? null;
  }
  return outermost;
}

export function selectOpenPageLocked(s: LockState): boolean {
  return selectOpenPageLockedBy(s) !== null;
}

/** Right tabs bound to the open document — not offered while it is locked. */
export const PAGE_BOUND_TABS = ['notes', 'refs', 'checkpoint', 'links', 'access', 'find'] as const;

/** Locked pages whose children the tree must not show: locked, and not opened by PIN. */
export function hiddenBranchIds(lockedIds: string[], unlockedDocIds: string[]): Set<string> {
  return new Set(lockedIds.filter(id => !unlockedDocIds.includes(id)));
}

// INVARIANT(security): a locked page not opened by PIN shows in the tree WITHOUT its
// children (no chevron, no keyboard path, no inbox mark rising from below). Why: operator
// request — before the PIN only the locked page itself is visible in the tree.
// Identity-preserving: an untouched subtree returns the same node objects, so the row
// memo (DocumentTreeItem INVARIANT) still skips re-renders.
export function pruneLockedBranches(nodes: DocumentTreeNode[], hidden: Set<string>): DocumentTreeNode[] {
  if (hidden.size === 0) return nodes;
  let changed = false;
  const out = nodes.map(n => {
    if (hidden.has(n.document_id)) {
      if (n.children.length === 0) return n;
      changed = true;
      return { ...n, children: [] };
    }
    const kids = pruneLockedBranches(n.children, hidden);
    if (kids === n.children) return n;
    changed = true;
    return { ...n, children: kids };
  });
  return changed ? out : nodes;
}

/** `docId` is `rootId` or lies below it (parent walk, stops on a revisit). */
export function isInSubtreeOf(documents: Document[], docId: string, rootId: string): boolean {
  const parents = buildParentMap(documents);
  const seen = new Set<string>();
  let cur: string | null = docId;
  while (cur && !seen.has(cur)) {
    if (cur === rootId) return true;
    seen.add(cur);
    cur = parents.get(cur) ?? null;
  }
  return false;
}
