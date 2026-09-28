/**
 * Pure routing decision for the navigate-to-reference handler.
 *
 * SYSTEM: reference-navigation — cross-doc vs same-doc routing decision for a
 * reference id; the side effects are document-navigation's
 *
 * Extracted from useEditorEvents so the cross-doc vs same-doc classification — the
 * crux of the ref-link navigation revert(root causes 1) —
 * is unit-testable without a React hook harness. The hook keeps the side effects
 * (setPendingReference, navigate, setState); this function owns ONLY the routing
 * decision.
 *
 * Why "same document" is often actually cross-doc: references are ANCESTOR-SCOPED
 * (backend/routes/references.py get_ancestor_ids), so a ref visible in D1's panel
 * frequently has document_id === <ancestor> !== D1. The cross-doc branch fires
 * whenever ref.document_id !== currentDocId — including the ancestor-owned case a
 * user perceives as "in my document". This is why the cross-doc revert (root cause 1)
 * explains both reported scenarios.
 *
 * INVARIANT: the same-doc decision MUST stay side-effect-free at the routing layer.  Why: hydrateReference (called before this) already commits currentReference for both paths; the same-doc decision here must stay side-effect-free so routing doesn't double-commit.
 * hydrateReference (called before this function in the handler) already commits
 * currentReference for both the in-store path (immediate marker + guarded resolve)
 * and the id-only path (guarded resolve). An unguarded post-await setCurrentReference
 * here would bypass hydrate's hydrateLastId latest-wins guard and yank the selection
 * back on a rapid double-click (root cause 2). The cross-doc branch's set is the only
 * intentional exception — it is idempotent and needed for setPendingReference + navigate.
 */
import type { Reference } from '../types';
import { refIsScope, type RefOpenMode } from '../store/ui-store/documents-slice';

export type ReferenceNavDecision =
  | { kind: 'noop' }
  | { kind: 'stay-in-context' }
  | { kind: 'same-doc' }
  | { kind: 'cross-doc'; targetDocId: string };

/**
 * Decide how the navigate-to-reference handler should treat a hydrated ref.
 *
 * @param ref          The hydrated reference (null if the hydrate resolved to nothing).
 * @param currentDocId The document currently open in the editor (undefined on cold start).
 * @param stayInContext When true, populate the reference column WITHOUT navigating.
 * @param refOpenMode  The CURRENT document's reference open mode (default 'center').
 *                     In 'panel' (quick preview) every ref stays in the document's
 *                     Refs tab — the document is the scope, so no navigation ever.
 */
export function resolveReferenceNav(params: {
  ref: Reference | null;
  currentDocId: string | undefined;
  stayInContext: boolean;
  refOpenMode?: RefOpenMode;
}): ReferenceNavDecision {
  const { ref, currentDocId, stayInContext, refOpenMode = 'center' } = params;
  if (!ref) return { kind: 'noop' };
  // stayInContext short-circuits before the doc-ownership check: it commits the ref
  // into the split-view right column (or swaps the single-view center) without any
  // document navigation. See useEditorEvents invariant.
  // Panel quick preview: same outcome via the ONE projection — the ref opens inside
  // the document's Refs tab regardless of ownership, so the cross-doc branch below
  // (and its parent-doc navigation) never fires.
  if (stayInContext || !refIsScope(refOpenMode)) return { kind: 'stay-in-context' };
  // Cross-doc when the ref is owned by a different (or ancestor) document. The falsy
  // guard on parentDocId covers null/empty owning docs (degenerate: nothing to nav to).
  const parentDocId = ref.document_id;
  if (parentDocId && parentDocId !== currentDocId) {
    return { kind: 'cross-doc', targetDocId: parentDocId };
  }
  return { kind: 'same-doc' };
}
