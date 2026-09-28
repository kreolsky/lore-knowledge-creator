/**
 * Public-share ReferencesPanel scope projection.
 *
 * see SYSTEM: public-share — display-time half of the public panel's reference
 * list. The public payload (`publicReferencesByDoc`) is intentionally
 * WHOLE-SUBTREE (public transclusion seeds `transcludeMap`/`validRefIds` from
 * the store list — there is no anonymous per-ref GET), but the panel must render
 * editor-parity scope: refs of the open doc + its in-subtree ancestor chain.
 *
 * This is a PURE PROJECTION: order-preserving filter to
 * `{docId} ∪ ancestorIds(documents, docId)` — never a re-sort, never a mutation.
 * The backend depth-tier sort (`sort_refs_by_depth_tier`) already yields the
 * exact desired order for the own+ancestor subset (own batch newest-first →
 * parent batch → … → share root); filtering keeps that order verbatim.
 *
 * The ancestor walk stops at the share root: the public tree's root node has
 * `parent_id === null` (server-normalized), and `ancestorIds` is cycle-guarded
 * with a missing-parent stop — no out-of-scope ids can leak in.
 */

import type { Document, Reference } from '../types';
import { ancestorIds } from './document-ancestors';

/**
 * Filter the whole-subtree public payload down to the panel's display scope.
 *
 * @param references store list (whole shared subtree, backend tier order) —
 *   NOT modified
 * @param documents the loaded public tree rows (metadata-only is enough)
 * @param docId the open document id; null/unknown → [] (nothing to project)
 * @returns a new array of the SAME Reference objects, payload order preserved
 */
export function publicPanelReferences(
  references: Reference[],
  documents: Document[],
  docId: string | null,
): Reference[] {
  if (!docId) return [];
  const scope = new Set<string>([docId, ...ancestorIds(documents, docId)]);
  // Null document_id (project-level ref) never exists on the public payload;
  // dropping it keeps the projection strict rather than trusting the wire.
  return references.filter(r => r.document_id !== null && scope.has(r.document_id));
}
