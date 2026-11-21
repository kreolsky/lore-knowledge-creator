/**
 * Document hierarchy walks over the flat `documents` list — parent map, children
 * map, descendants, ancestors, subtree membership. Every walk stops on a revisit,
 * so a cyclic parent_id still terminates, and a missing parent row ends the walk
 * without throwing.
 *
 * // SYSTEM: document-hierarchy — parent/children maps, descendant and ancestor walks over the flat document list
 */

import type { Document } from '../types';

export type ParentMap = Map<string, string | null>;
export type ChildrenMap = Map<string, string[]>;

export function buildParentMap(documents: Document[]): ParentMap {
  const map: ParentMap = new Map();
  for (const d of documents) {
    map.set(d.document_id, d.parent_id);
  }
  return map;
}

export function childrenMap(parentMap: ParentMap): ChildrenMap {
  const children: ChildrenMap = new Map();
  for (const [id, parent] of parentMap) {
    if (parent === null) continue;
    const siblings = children.get(parent) ?? [];
    siblings.push(id);
    children.set(parent, siblings);
  }
  return children;
}

/** Every document below any of `rootIds` — the roots themselves only with
 * `includeRoots`. Takes a children map so a caller walking many roots builds it once. */
export function descendantIds(
  rootIds: Iterable<string>,
  children: ChildrenMap,
  { includeRoots }: { includeRoots: boolean },
): Set<string> {
  const result = new Set<string>();
  const stack: string[] = [];
  for (const root of rootIds) {
    if (includeRoots) result.add(root);
    stack.push(root);
  }
  while (stack.length > 0) {
    const node = stack.pop()!;
    for (const child of children.get(node) ?? []) {
      if (result.has(child)) continue;
      result.add(child);
      stack.push(child);
    }
  }
  return result;
}

/**
 * Ancestor chain of a document, ids ROOT-FIRST (top-down: project-root ancestor …
 * immediate parent). Used by reveal-in-tree to expand collapsed ancestors of the
 * focused document. Returns [] for a root document or an unknown id.
 */
export function ancestorIds(documents: Document[], docId: string): string[] {
  const byId = new Map<string, Document>();
  for (const d of documents) byId.set(d.document_id, d);

  const chain: string[] = [];
  const seen = new Set<string>();
  let parentId = byId.get(docId)?.parent_id ?? null;
  while (parentId !== null && !seen.has(parentId)) {
    seen.add(parentId);
    const node = byId.get(parentId);
    if (!node) break;
    chain.unshift(parentId);
    parentId = node.parent_id;
  }
  return chain;
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
