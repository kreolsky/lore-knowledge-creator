/**
 * Tiered sorting for document picker search results.
 *
 * Sort key priority: Name Match → Proximity (tree distance) → Recency.
 * // SYSTEM: document-sort — picker result ranking
 */

import type { Document, Reference } from '../types';

export function buildParentMap(documents: Document[]): Map<string, string | null> {
  const map = new Map<string, string | null>();
  for (const d of documents) {
    map.set(d.document_id, d.parent_id);
  }
  return map;
}

// ARCH: Project itself is a virtual root above all documents — disconnected
// subtrees still have a finite distance via the project node.
// EXPORTED: the chat-session proximity
// comparator reuses the same root sentinel + tree-walk as the picker sort, so a
// doc-less chat session ranks by PROJECT_ROOT distance (finite) instead of being
// a special case. Keep behavior unchanged — only the export surface widened.
export const PROJECT_ROOT = '\0__project__';

export function treeDistance(
  docA: string,
  docB: string,
  parentMap: Map<string, string | null>,
): number {
  if (docA === docB) return 0;

  const depthsB = new Map<string, number>();
  let cur: string | null = docB;
  let d = 0;
  while (cur) {
    depthsB.set(cur, d++);
    cur = parentMap.get(cur) ?? null;
  }
  depthsB.set(PROJECT_ROOT, d);

  cur = docA;
  d = 0;
  while (cur) {
    const db = depthsB.get(cur);
    if (db !== undefined) return d + db;
    cur = parentMap.get(cur) ?? null;
    d++;
  }
  return d + (depthsB.get(PROJECT_ROOT) ?? 0);
}

type NameMatchTier = 0 | 1 | 2;

function nameMatchTier(title: string, query: string): NameMatchTier {
  const t = title.toLowerCase();
  const q = query.toLowerCase();
  if (t === q) return 0;
  if (t.startsWith(q)) return 1;
  return 2;
}

// ARCH: Sort key priority — Name Match (tier 0/1/2) > Proximity (tree steps) > Recency (updated_at DESC)
export function sortDocuments(
  docs: Document[],
  currentDocId: string | null,
  query: string,
  parentMap: Map<string, string | null>,
): Document[] {
  return [...docs].sort((a, b) => {
    const tierA = nameMatchTier(a.title, query);
    const tierB = nameMatchTier(b.title, query);
    if (tierA !== tierB) return tierA - tierB;

    if (currentDocId) {
      const distA = treeDistance(currentDocId, a.document_id, parentMap);
      const distB = treeDistance(currentDocId, b.document_id, parentMap);
      if (distA !== distB) return distA - distB;
    }

    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}

export function sortReferences(refs: Reference[], query: string): Reference[] {
  return [...refs].sort((a, b) => {
    const tierA = nameMatchTier(a.title, query);
    const tierB = nameMatchTier(b.title, query);
    if (tierA !== tierB) return tierA - tierB;
    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}

// INVARIANT: picker tiebreaker mirrors the right-panel tree order (sort_key ASC, then
// document_id) — same comparator as `sortSiblings` in app-store.ts.
// Why: pickers must present siblings in the order the user deliberately arranged via
// drag-reorder, not alphabetically or by recency.
function compareBySortKey(a: Document, b: Document): number {
  const ka = a.sort_key ?? '';
  const kb = b.sort_key ?? '';
  if (ka !== kb) return ka < kb ? -1 : 1;
  return a.document_id < b.document_id ? -1 : a.document_id > b.document_id ? 1 : 0;
}

function descendantsOf(anchorId: string, parentMap: Map<string, string | null>): Set<string> {
  const childrenMap = new Map<string, string[]>();
  for (const [id, parent] of parentMap) {
    if (parent !== null) {
      const siblings = childrenMap.get(parent) ?? [];
      siblings.push(id);
      childrenMap.set(parent, siblings);
    }
  }
  const result = new Set<string>([anchorId]);
  const stack = [anchorId];
  while (stack.length > 0) {
    const node = stack.pop()!;
    for (const child of childrenMap.get(node) ?? []) {
      result.add(child);
      stack.push(child);
    }
  }
  return result;
}

// ARCH: Chat picker sort — anchor doc → tree distance → descendants-first → name-match → sort_key (tree order).
// Name-match preserves search relevance within proximity tiers; the final tiebreaker mirrors
// the right-panel tree order (sort_key) so ties surface in the user's arranged order.
export function sortDocumentsForChat(
  docs: Document[],
  anchorDocId: string | null,
  parentMap: Map<string, string | null>,
  query: string = '',
): Document[] {
  if (!anchorDocId) {
    return [...docs].sort((a, b) => {
      const tierA = nameMatchTier(a.title, query);
      const tierB = nameMatchTier(b.title, query);
      if (tierA !== tierB) return tierA - tierB;
      return compareBySortKey(a, b);
    });
  }
  const descendants = descendantsOf(anchorDocId, parentMap);
  return [...docs].sort((a, b) => {
    const selfA = a.document_id === anchorDocId ? 0 : 1;
    const selfB = b.document_id === anchorDocId ? 0 : 1;
    if (selfA !== selfB) return selfA - selfB;

    const distA = treeDistance(anchorDocId, a.document_id, parentMap);
    const distB = treeDistance(anchorDocId, b.document_id, parentMap);
    if (distA !== distB) return distA - distB;

    const kinA = descendants.has(a.document_id) ? 0 : 1;
    const kinB = descendants.has(b.document_id) ? 0 : 1;
    if (kinA !== kinB) return kinA - kinB;

    const tierA = nameMatchTier(a.title, query);
    const tierB = nameMatchTier(b.title, query);
    if (tierA !== tierB) return tierA - tierB;

    return compareBySortKey(a, b);
  });
}

// ARCH: Chat picker reference sort — selected-first, then anchor ref, then
// name-match (query only), then parent-document proximity to the anchor, then
// updated_at DESC (newest first within a doc). The open document's reference
// scope is NEVER consulted — the picker lists ALL project references (Part B),
// so a cross-doc ref must sort by its owning document's tree distance, and a
// selected ref always stays in view regardless of query/anchor/proximity.
// INVARIANT: selected references ALWAYS sort above everything else. Why: the
// user's current selection must remain visible while the picker is open; the
// tier is keyed by the picker's frozen selection set so the list does not
// reshuffle on every (un)check.
export function sortReferencesByProximity(
  refs: Reference[],
  anchorDocId: string | null,
  anchorRefId: string | null,
  selectedRefIds: Set<string>,
  parentMap: Map<string, string | null>,
  query: string = '',
): Reference[] {
  const q = query.toLowerCase();
  // Distance from the anchor document to a reference's owning document. A
  // project-level ref (document_id === null) is treated as project-root
  // distance — treeDistance is (string, string), so PROJECT_ROOT stands in for
  // null and must never be replaced by a real null arg.
  const distance = (ref: Reference): number | null =>
    anchorDocId === null
      ? null
      : treeDistance(anchorDocId, ref.document_id ?? PROJECT_ROOT, parentMap);

  return [...refs].sort((a, b) => {
    const selA = selectedRefIds.has(a.reference_id) ? 0 : 1;
    const selB = selectedRefIds.has(b.reference_id) ? 0 : 1;
    if (selA !== selB) return selA - selB;

    const ancA = a.reference_id === anchorRefId ? 0 : 1;
    const ancB = b.reference_id === anchorRefId ? 0 : 1;
    if (ancA !== ancB) return ancA - ancB;

    if (q) {
      const nA = nameMatchTier(a.title, query);
      const nB = nameMatchTier(b.title, query);
      if (nA !== nB) return nA - nB;
    }

    const dA = distance(a);
    const dB = distance(b);
    if (dA !== null && dB !== null && dA !== dB) return dA - dB;

    return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime();
  });
}
