/**
 * Ancestor chain of a document from the flat `documents` list.
 *
 * Walks `parent_id` upward, returning ids ROOT-FIRST (top-down: project-root
 * ancestor … immediate parent). Used by reveal-in-tree to expand collapsed
 * ancestors of the focused document. Cycle-guarded; a missing parent row stops
 * the walk without throwing. Returns [] for a root document or an unknown id.
 *
 * // SYSTEM: document-ancestors — ancestor-id walk for tree reveal
 */

import type { Document } from '../types';

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
