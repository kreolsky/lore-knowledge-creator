/** Page PIN lock (SYSTEM: page-lock) — the one "is the open page still behind its PIN" projection. */
import type { AppState } from '../store/app-store';
import type { Document, DocumentTreeNode } from '../types';
import { buildParentMap } from './document-sort';

type LockState = Pick<AppState, 'currentDocument' | 'currentUser' | 'unlockedDocIds' | 'pageLockLeftAt' | 'documents'>;

/** Gate value while the project's document list (the parent chain) has not arrived yet. */
export const LOCK_PENDING = ':pending';

// WHY a leaf module, not app-store: its readers' tests mock app-store wholesale, and a
// selector exported from the mocked module would be missing there.
// INVARIANT(security): the open page is gated by the OUTERMOST locked, not-PIN-opened page on its parent chain.
// Why: operator request — a child of a locked page reopened by URL or reload must show
// the locked ancestor's PIN screen, not its own text. The chain includes the page itself;
// while it is unknown (a reload commits the document one RTT before the list) the gate is
// PENDING, never open. Users with no locks walk nothing.
// Grace widening (page-lock-grace): "PIN-opened" means opened and not past the admin-set
// grace window — leaving a page starts the window, being back deletes it, and a reload or
// a lapsed window shows the PIN screen exactly as before (stamps are in-tab memory only).

/** The effective "opened" set at `now`: PIN-entered ids whose leave-stamp is absent (back
 * in the page's subtree) or younger than the grace window. `graceS` null (login payload)
 * or 0 closes any stamped id — fail-safe. Feeds BOTH the gate and the tree pruning, so
 * the two can never disagree during grace. The `??` guards are for the readers'
 * wholesale app-store mocks (partial state — see WHY above), not for production. */
export function effectiveUnlockedDocIds(
  s: Pick<AppState, 'currentUser' | 'unlockedDocIds' | 'pageLockLeftAt'>,
  now: number,
): string[] {
  const graceS = s.currentUser?.page_lock_grace_s ?? null;
  const leftAt = s.pageLockLeftAt ?? {};
  return (s.unlockedDocIds ?? []).filter(id => {
    const at = leftAt[id];
    return at === undefined || (!!graceS && now - at < graceS * 1000);
  });
}

/** The OUTERMOST locked, not-PIN-opened page on `docId`'s parent chain, or null. */
export function selectDocLockedBy(s: Omit<LockState, 'currentDocument'>, docId: string): string | null {
  const locked = s.currentUser?.locked_doc_ids;
  if (!locked || locked.length === 0) return null;
  const unlocked = effectiveUnlockedDocIds(s, Date.now());
  const parents = buildParentMap(s.documents);
  const seen = new Set<string>();
  let outermost: string | null = null;
  let cur: string | null = docId;
  while (cur && !seen.has(cur)) {
    if (locked.includes(cur) && !unlocked.includes(cur)) outermost = cur;
    seen.add(cur);
    cur = parents.get(cur) ?? null;
  }
  return outermost;
}

export function selectOpenPageLockedBy(s: LockState): string | null {
  const id = s.currentDocument?.document_id;
  const locked = s.currentUser?.locked_doc_ids;
  if (!id || !locked || locked.length === 0) return null;
  if (!s.documents.some(d => d.document_id === id)) return LOCK_PENDING;
  return selectDocLockedBy(s, id);
}

export function selectOpenPageLocked(s: LockState): boolean {
  return selectOpenPageLockedBy(s) !== null;
}

/** Right tabs bound to the open document — not offered while it is locked. */
export const PAGE_BOUND_TABS = ['notes', 'refs', 'checkpoint', 'links', 'access', 'find'] as const;

/** Locked pages whose children the tree must not show: locked, and not effectively opened
 * (PIN-entered and not past grace). Callers pass `effectiveUnlockedDocIds` output — the
 * SAME set the gate reads — so a branch never stays hidden while its page renders open. */
export function hiddenBranchIds(lockedIds: string[], unlockedDocIds: string[]): Set<string> {
  return new Set(lockedIds.filter(id => !unlockedDocIds.includes(id)));
}

// INVARIANT(security): a locked page not opened by PIN shows in the tree WITHOUT its
// children (no chevron, no keyboard path, no inbox mark rising from below). Why: operator
// request — before the PIN only the locked page itself is visible in the tree. "Opened"
// is the effective set (within the grace window), same predicate as the gate above.
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
