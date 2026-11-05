/** Which document-tree rows carry a highlight plaque, derived once for the whole tree. */
import type { DocumentTreeNode } from '../types';

export interface TreeHighlight {
  activeId: string | null;
  refParentId: string | null;
  refSourceId: string | null;
  snapshotParentId: string | null;
  /** Highlighted rows and their ancestors: the only rows that must receive a change. */
  path: ReadonlySet<string>;
}

interface TreeHighlightInput {
  routeDocId: string | undefined;
  openDocId: string | undefined;
  hasScopeReference: boolean;
  scopeReferenceDocId: string | null | undefined;
  referenceSourceDocId: string | null | undefined;
  snapshotDocId: string | undefined;
}

export const NO_HIGHLIGHT: TreeHighlight = {
  activeId: null,
  refParentId: null,
  refSourceId: null,
  snapshotParentId: null,
  path: new Set(),
};

export function deriveTreeHighlight(tree: DocumentTreeNode[], input: TreeHighlightInput): TreeHighlight {
  const { routeDocId, openDocId, hasScopeReference, scopeReferenceDocId, referenceSourceDocId, snapshotDocId } = input;
  // INVARIANT: highlight keys off the route param when present, else the open
  // doc's id. Why: on /s/:token the URL is the token (no /docs/:id param), so
  // `documentId` is undefined and the selected sub-doc got no plaque. Authed is
  // unaffected — there `documentId === currentDocument.document_id`.
  const openId = routeDocId ?? openDocId ?? null;
  const refParentId = hasScopeReference ? scopeReferenceDocId ?? null : null;
  const refSourceCandidate = hasScopeReference ? referenceSourceDocId || openId : null;
  // INVARIANT: a row is never both the reference's source and its parent — the
  // parent plaque (blue) wins. Why: both `active` and `ref-parent` on one row
  // lets the later `.active .doc-item-actions` rule paint a gray plate under the
  // buttons of a blue row (reference opened from its own parent doc).
  const refSourceId = refSourceCandidate === refParentId ? null : refSourceCandidate;
  // INVARIANT: Snapshot-parent highlight hides while a reference is open.
  // Why: reference viewer takes visual precedence over snapshot preview.
  const snapshotParentId = hasScopeReference ? null : snapshotDocId ?? null;
  const marked = { activeId: hasScopeReference ? null : openId, refParentId, refSourceId, snapshotParentId };
  const ids = new Set(Object.values(marked));
  const path = new Set<string>();
  const walk = (nodes: DocumentTreeNode[]): boolean => {
    let found = false;
    for (const node of nodes) {
      const hit = walk(node.children) || ids.has(node.document_id);
      if (hit) path.add(node.document_id);
      found ||= hit;
    }
    return found;
  };
  walk(tree);
  return { ...marked, path };
}

/** What one row receives: the shared constant unless its branch holds a highlight. */
export function highlightForRow(highlight: TreeHighlight, documentId: string): TreeHighlight {
  return highlight.path.has(documentId) ? highlight : NO_HIGHLIGHT;
}
