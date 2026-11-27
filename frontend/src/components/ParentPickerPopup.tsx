/**
 * Floating popup for changing a document's parent.
 *
 * The search + list + preview body is DocumentPickerPopup; this wrapper supplies the
 * exclusions (the target document and all its descendants to prevent cycles, and the
 * Memory folder with its cards — never a destination, on every call site), the
 * "no parent" lead item and the reparent on select.
 */

import { useMemo } from 'react';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { serverRefusalDetail } from '../utils/server-refusal-detail';
import { useTranslation } from '../i18n';
import { DocumentPickerPopup } from './DocumentPickerPopup';
import { buildParentMap, childrenMap, descendantIds, type ChildrenMap } from '../utils/document-hierarchy';
import type { Document, DocumentTreeNode } from '../types';

/** The Memory folder and every card under it. */
function getMemoryIds(documents: Document[], children: ChildrenMap): Set<string> {
  const folder = documents.find(d => d.system_role === 'memory_folder');
  if (!folder) return new Set<string>();
  return descendantIds([folder.document_id], children, { includeRoots: true });
}

interface Props {
  /** Document reparent mode */
  doc?: DocumentTreeNode;
  /** Reference reparent mode — current document_id of the reference */
  currentDocumentId?: string | null;
  /** Callback for reference mode — receives new document_id (null = project-level) */
  onMoved?: (newDocumentId: string | null) => void;
  anchorRect: DOMRect;
  onClose: () => void;
  /** Open above anchor instead of below (default: false) */
  above?: boolean;
  /** Hide preview panel (default: false) */
  hidePreview?: boolean;
  /**
   * Override the document list (default: the app store's documents). Used by
   * cross-project surfaces (AccessPanel move section) that feed the picker a
   * TARGET project's tree instead of the current project's.
   */
  documents?: Document[];
  /** Override the "no parent" sentinel label (default: mode-dependent t() key). */
  noParentLabel?: string;
}

export function ParentPickerPopup({ doc, currentDocumentId, onMoved, anchorRect, onClose, above, hidePreview, documents: documentsProp, noParentLabel: noParentLabelProp }: Props) {
  const { t } = useTranslation();
  const isRefMode = !doc;
  // Hook called unconditionally (rules-of-hooks); the prop merely overrides it.
  const storeDocuments = useAppStore(s => s.documents);
  const documents = documentsProp ?? storeDocuments;
  const setDocuments = useAppStore(s => s.setDocuments);
  const showToast = useAppStore(s => s.showToast);

  const parentMap = useMemo(() => buildParentMap(documents), [documents]);
  const children = useMemo(() => childrenMap(parentMap), [parentMap]);

  // INVARIANT: the Memory folder and its cards are never offered as a parent.
  // Why: a document filed there silently becomes memory the agent retrieves; the
  // server refuses the move from outside memory (documents/move.py).
  // The moved document itself and its descendants are excluded too (cycles).
  const excludedIds = useMemo(
    () => new Set([
      ...(doc ? descendantIds([doc.document_id], children, { includeRoots: true }) : []),
      ...getMemoryIds(documents, children),
    ]),
    [doc, documents, children],
  );

  const currentParentId = doc ? (doc.parent_id ?? '') : (currentDocumentId ?? '');
  const noParentLabel = noParentLabelProp
    ?? (isRefMode ? t('projectLevelAllDocs') : t('noParentTopLevel'));

  const handleSelect = async (parentId: string) => {
    if (isRefMode) {
      onMoved?.(parentId || null);
      onClose();
      return;
    }
    // Warn when a reparent would move the doc (and its descendants) OUT of a
    // scoped agent key's subtree — those keys silently lose access to the moved
    // branch. Moving a subtree ROOT itself is fine (descendants follow, the
    // subtree stays internally consistent). Scope roots are the caller's own
    // docs carrying the agent capability in the server-enriched documents
    // payload (plan "glimmering-knitting-pebble") — no fetch, no silent
    // degradation path.
    const roots = documents
      .filter(d => d.key_capabilities?.includes('agent'))
      .map(d => d.document_id);
    if (roots.length) {
      const movedId = doc!.document_id;
      // The new parent's subtree membership is what decides the crossing.
      const crossing = roots.filter(rootId => {
        const subtree = descendantIds([rootId], children, { includeRoots: true });
        return subtree.has(movedId) && !subtree.has(parentId);
      });
      if (crossing.length) {
        const ok = window.confirm(t('agentKeyReparentWarn', { count: crossing.length }));
        if (!ok) { onClose(); return; }
      }
    }
    try {
      const updatedDoc = await apiClient.patch(`/documents/${doc!.document_id}`, {
        parent_id: parentId,
      });
      const docs = useAppStore.getState().documents;
      setDocuments(docs.map(d =>
        d.document_id === doc!.document_id ? updatedDoc : d,
      ));
    } catch (err) {
      // WHY: the popup stays open on failure so the user can pick another parent;
      // the server's reason (cycle, Memory) names why this one was refused.
      showToast(serverRefusalDetail(err) ?? t('changeParentFailed'), 'error');
      return;
    }
    onClose();
  };

  return (
    <DocumentPickerPopup
      documents={documents}
      excludedIds={excludedIds}
      leadLabel={noParentLabel}
      currentId={currentParentId}
      anchorDocId={doc?.document_id ?? currentDocumentId ?? null}
      onSelect={handleSelect}
      anchorRect={anchorRect}
      onClose={onClose}
      above={above}
      hidePreview={hidePreview}
    />
  );
}
