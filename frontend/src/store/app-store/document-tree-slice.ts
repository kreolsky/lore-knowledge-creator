/** Document-tree slice — the flat `documents` list and the `documentTree` derived from it.

# ARCH (debt-paydown W6): owns the flat list + its tree projection together, because they
# must be written in ONE `set` — a render that sees a new list against the previous tree
# shows the sidebar and the editor disagreeing about which documents exist.
#
# NOT here: `setCurrentProject` (clears documents/documentTree as part of the project-switch
# transition it owns) and `setCurrentDocument` (entangled with the references prefetch).
# A slice must not own half of another slice's transition.
*/
import type { Document, DocumentTreeNode } from '../../types';
import type { AppState } from '../app-store';

// INVARIANT: siblings are ordered by sort_key ASC, document_id tie-breaking equal
// keys from concurrent drags. Why: sort_key is the persisted manual/newest-first
// order (backend ORDER BY matches); array arrival order is not authoritative.
function sortSiblings(nodes: DocumentTreeNode[]): DocumentTreeNode[] {
  return nodes.sort((a, b) => {
    const ka = a.sort_key ?? '';
    const kb = b.sort_key ?? '';
    if (ka !== kb) return ka < kb ? -1 : 1;
    return a.document_id < b.document_id ? -1 : a.document_id > b.document_id ? 1 : 0;
  });
}

/** Structural equality for JSON-shaped values (scalars, arrays, plain objects). */
function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => sameValue(v, b[i]));
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ka = Object.keys(a as object);
    const kb = Object.keys(b as object);
    if (ka.length !== kb.length) return false;
    return ka.every(k => sameValue((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
  }
  return false;
}

export function buildDocumentTree(documents: Document[]): DocumentTreeNode[] {
  const byId = new Map<string, DocumentTreeNode>();
  for (const d of documents) {
    byId.set(d.document_id, { ...d, children: [] });
  }
  const roots: DocumentTreeNode[] = [];
  for (const d of documents) {
    if (d.is_index) continue;
    const node = byId.get(d.document_id)!;
    if (d.parent_id && byId.has(d.parent_id)) {
      byId.get(d.parent_id)!.children.push(node);
    } else if (!d.parent_id) {
      roots.push(node);
    }
  }
  for (const node of byId.values()) sortSiblings(node.children);
  return sortSiblings(roots);
}

export type DocumentTreeSlice = Pick<AppState, 'documents' | 'documentTree' | 'setDocuments'>;

type AppSet = (
  partial: Partial<AppState> | ((state: AppState) => Partial<AppState> | AppState),
) => void;
type AppGet = () => AppState;

export function createDocumentTreeSlice(set: AppSet, get: AppGet): DocumentTreeSlice {
  return {
    documents: [],
    documentTree: [],
    setDocuments: (documents) => {
      const { documents: prev, documentTree: prevTree } = get();
      // sort_key included so a pure reorder (same ids/parents) still rebuilds the tree.
      const fingerprint = (docs: Document[]) =>
        docs.map(d => `${d.document_id}:${d.parent_id ?? ''}:${d.is_index}:${d.sort_key ?? ''}`).join('|');
      if (fingerprint(documents) !== fingerprint(prev)) {
        set({ documents, documentTree: buildDocumentTree(documents) });
      } else {
        // Structure unchanged ⇒ patch node CONTENT in place and reuse every untouched
        // node/array identity. Why: a full rebuild hands React new objects for the whole
        // tree, re-rendering every sidebar row on each title/content poll.
        // The comparison below walks the key UNION of each document, never a hand-listed
        // field set: the flat list also feeds `last_save_failed_at` (save-failure marker)
        // and `key_capabilities` (doc-tree key icon), which patchTree's
        // title/content/headings subset would freeze silently. Index-wise is sound — the
        // fingerprint above already pinned identical ids in identical order.
        // INVARIANT: a `setDocuments` that changes NOTHING leaves both `documents` and
        // `documentTree` at their previous identity — no `set` at all.
        // Why: ~10 components and every sidebar row subscribe to the flat array BY
        // REFERENCE, so a fresh array from an idle poll (or the WS echo of an
        // already-applied delete) re-rendered the whole app — the create/delete flash.
        if (documents.length === prev.length && documents.every((d, i) => sameValue(d, prev[i]))) return;

        const docMap = new Map(documents.map(d => [d.document_id, d]));
        const patchTree = (nodes: DocumentTreeNode[]): DocumentTreeNode[] => {
          let changed = false;
          const result = nodes.map(node => {
            const doc = docMap.get(node.document_id);
            const children = patchTree(node.children);
            if (!doc || (doc.title === node.title && doc.content === node.content && doc.headings === node.headings && children === node.children)) return node;
            changed = true;
            return { ...node, ...doc, children };
          });
          return changed ? result : nodes;
        };
        set({ documents, documentTree: patchTree(prevTree) });
      }
    },
  };
}
