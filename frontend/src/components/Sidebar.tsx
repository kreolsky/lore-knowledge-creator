/**
 * Sidebar: document tree with nested hierarchy, inline rename, create/delete actions.
 *
 * Orchestrates document CRUD, WS-driven tree updates, and delegates rendering
 * to DocumentTree. Delete/parent-picker modals managed here.
 *
 * Store slices: documentTree, currentDocument, sidebarTab.
 *
 * PUBLIC SHARE: when `isPublicShare` (from ui-store), the authed /projects/:id
 * fetch is skipped (the tree is pre-hydrated from publicTree by PublicSharePage)
 * and the create-doc event handler is disabled. The CRUD handlers are already
 * gated by `accessLevel === 'full'` and no-op under readonly, but the create
 * handler also emits navigate-to-document which would route into /projects/…
 * (401) — so it must be explicitly disabled.
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { emit } from '../events';
import { useEvent } from '../hooks/useEvent';
import { useDocumentRoute } from '../hooks/useDocumentRoute';
import { DeleteModal } from './DeleteModal';
import { ParentPickerPopup } from './ParentPickerPopup';
import { DocumentTree } from './DocumentTree';
import { DocumentTreeNode } from '../types';
import { docUrl } from '../utils/routing';
import { useTranslation } from '../i18n';

export function Sidebar() {
  const { t } = useTranslation();
  // WHY: a ref, not a dep — the load runs on mount/route change, never on a language switch.
  const tRef = useRef(t);
  tRef.current = t;
  const { projectId, documentId } = useDocumentRoute();
  const navigate = useNavigate();
  const isPublicShare = useUIStore(s => s.isPublicShare);
  const accessLevel = useAppStore(s => s.accessLevel);
  const { documents, setDocuments } = useAppStore(useShallow(s => ({ documents: s.documents, setDocuments: s.setDocuments })));
  const canEdit = !isPublicShare && accessLevel === 'full';
  const [deleteModal, setDeleteModal] = useState<{
    type: 'project' | 'document', id: string, name: string,
    subtree?: { ids: string[]; childDocs: number; childRefs: number },
  } | null>(null);
  const [parentPicker, setParentPicker] = useState<{ doc: DocumentTreeNode; anchorRect: DOMRect } | null>(null);

  const handleDelete = useCallback(async (id: string, name: string) => {
    // Subtree census for the modal's "delete the whole subtree" checkbox: BFS over
    // the live tree docs (always hydrated) plus references HOSTED inside the
    // subtree (a reference's document_id is its host doc — its tree parent).
    const docs = useAppStore.getState().documents;
    const ids = new Set<string>([id]);
    let frontier = [id];
    while (frontier.length) {
      const next: string[] = [];
      for (const d of docs) {
        if (d.parent_id && frontier.includes(d.parent_id) && !ids.has(d.document_id)) {
          ids.add(d.document_id);
          next.push(d.document_id);
        }
      }
      frontier = next;
    }
    const inSubtree = (refs: { document_id: string | null }[]) =>
      refs.filter(r => r.document_id && ids.has(r.document_id)).length;
    // Ref census comes from a project-wide fetch, NOT the references store: the
    // store holds the References panel's SCOPE-filtered list and is empty until
    // the panel opens — a store-based count showed "0 refs" for a fresh-opened
    // project (live-drive finding 2026-08-24). On fetch failure the count falls
    // back to the store-based best effort; the checkbox ACTION is unaffected —
    // the label is advisory, so a toast here would be noise, not signal.
    let childRefs = inSubtree(useAppStore.getState().references);
    if (projectId && !isPublicShare) {
      try {
        childRefs = inSubtree(
          await apiClient.get(`/references?project_id=${projectId}&limit=1000`),
        );
      } catch (err) {
        // WHY: log only — the count is an advisory label (see above); the delete itself is unaffected.
        console.error('Failed to count subtree references', err);
      }
    }
    const childDocs = ids.size - 1;
    setDeleteModal({
      type: 'document', id, name,
      ...(childDocs + childRefs > 0 ? { subtree: { ids: [...ids], childDocs, childRefs } } : {}),
    });
  }, [projectId, isPublicShare]);
  const handleCreateChild = useCallback((parentId: string) => {
    emit('open-create-doc', { parentId });
  }, []);
  const handleChangeParent = useCallback((doc: DocumentTreeNode, anchorRect: DOMRect) => {
    setParentPicker({ doc, anchorRect });
  }, []);

  // PUBLIC: documents are pre-hydrated from publicTree by PublicSharePage — skip
  // the authed /projects/:id fetch (would 401+toast on the anonymous surface).
  useEffect(() => {
    if (isPublicShare || !projectId) return;
    // INVARIANT: a remount with the tree already loaded for THIS project does not refetch.
    // Why: the shell remounts when routing crosses /projects/:id ↔ /docs/:id, and the
    // refetch repainted the whole tree — part of the reported full-screen flash. The
    // tree stays live through the project WS channel, so a warm store is current, not
    // stale. Read at effect time, not from the render closure: `documents` must not
    // become a dep or every tree change would re-trigger the fetch it just applied.
    const state = useAppStore.getState();
    if (state.currentProject?.project_id === projectId && state.documents.length > 0) return;
    apiClient.get(`/projects/${projectId}`).then((data) => {
      setDocuments(data.documents);
    }).catch((err) => {
      console.error('Failed to load the document tree', err);
      useAppStore.getState().showToast(tRef.current('failedToLoadTree'), 'error');
    });
  }, [projectId, setDocuments, isPublicShare]);

  // PUBLIC: WS tree events (created/renamed/moved/deleted) fire on the project
  // collab channel — the public page has no collab WS, so these never fire.
  // Subscribing is harmless (the bus is in-memory only without a socket).

  // Project WS: real-time tree updates from other users
  useEvent('ws:document_created', useCallback(({ document_id: docId, title, parent_id: parentId, sort_key: sortKey }) => {
    const docs = useAppStore.getState().documents;
    if (docs.some(d => d.document_id === docId)) return;
    setDocuments([...docs, { document_id: docId, title, parent_id: parentId, is_index: false, sort_key: sortKey ?? undefined } as typeof docs[0]]);
    if (parentId && useUIStore.getState().collapsedDocIds.includes(parentId)) {
      useUIStore.getState().toggleDocExpanded(parentId);
    }
  }, [setDocuments]));

  useEvent('ws:document_renamed', useCallback(({ document_id: docId, title }) => {
    const docs = useAppStore.getState().documents;
    setDocuments(docs.map(d => d.document_id === docId ? { ...d, title } : d));
  }, [setDocuments]));

  // Moved and reordered both carry the authoritative new sort_key — apply it so peers re-sort.
  // A CONVERSION (is_reference = the node's FINAL kind) additionally
  // moves the node BETWEEN the tree and the references panels: this list holds
  // non-reference docs only (GET /projects/:id filters is_reference=false), so a
  // doc→reference conversion must REMOVE the row (a kept row renders as a phantom
  // tree child until reload) and a reference→doc conversion must INSERT one
  // (title rides the payload — the only source this client has).
  useEvent('ws:document_moved', useCallback(({ document_id: docId, parent_id: parentId, sort_key: sortKey, is_reference: isReference, title }) => {
    const docs = useAppStore.getState().documents;
    const reparent = (list: typeof docs) => list.map(d => (
      d.document_id === docId ? { ...d, parent_id: parentId, ...(sortKey ? { sort_key: sortKey } : {}) } : d
    ));
    if (isReference) {
      if (docs.some(d => d.document_id === docId)) {
        setDocuments(docs.filter(d => d.document_id !== docId));
      }
      return;
    }
    if (!docs.some(d => d.document_id === docId)) {
      setDocuments([...docs, {
        document_id: docId, title: title ?? '', parent_id: parentId,
        is_index: false, sort_key: sortKey ?? undefined,
      } as typeof docs[0]]);
      return;
    }
    setDocuments(reparent(docs));
  }, [setDocuments]));

  useEvent('ws:document_reordered', useCallback(({ document_id: docId, sort_key: sortKey }) => {
    if (!sortKey) return;
    const docs = useAppStore.getState().documents;
    setDocuments(docs.map(d => d.document_id === docId ? { ...d, sort_key: sortKey } : d));
  }, [setDocuments]));

  useEvent('ws:document_deleted', useCallback(({ document_id: docId }) => {
    const docs = useAppStore.getState().documents;
    setDocuments(docs.filter(d => d.document_id !== docId));
    useUIStore.getState().removeDocState(docId);
  }, [setDocuments]));

  useEvent('ws:documents_deleted_batch', useCallback(({ document_ids: documentIds }) => {
    // ARCH: single filter for all deleted docs — replaces N individual document_deleted events.
    // Reference cleanup is handled by useReferenceEvents (always-mounted) via the same event.
    const idSet = new Set(documentIds);
    const docs = useAppStore.getState().documents;
    setDocuments(docs.filter(d => !idSet.has(d.document_id)));
    useUIStore.getState().removeDocStates(documentIds);
  }, [setDocuments]));

  // Cross-project subtree move, SOURCE side (SYSTEM: project-ws). The subtree
  // left this project: drop its ids from the tree exactly like the batch-delete
  // handler above. If the OPEN doc is among them, follow it to the target via a
  // HARD reload: the doc URL is project-less (/docs/<id>), so a full reload
  // re-resolves the new project, re-subscribes both WS channels and drops the
  // stale collab join — resetting currentProject + both sockets + the joined
  // set piecemeal is the bug surface this handler must not open.
  useEvent('ws:documents_moved_out', useCallback(({ document_ids: documentIds, target_project_name: targetProjectName }) => {
    const idSet = new Set(documentIds);
    const docs = useAppStore.getState().documents;
    setDocuments(docs.filter(d => !idSet.has(d.document_id)));
    useUIStore.getState().removeDocStates(documentIds);
    const openId = useAppStore.getState().currentDocument?.document_id ?? null;
    if (openId && idSet.has(openId)) {
      useAppStore.getState().showToast(
        t('documentMovedToProject', { project: targetProjectName }), 'info',
      );
      window.location.assign(docUrl(openId));
    }
  }, [setDocuments, t]));

  // Cross-project subtree move, TARGET side: another project's subtree just
  // arrived here. Refetch the whole tree (GET /projects/:id) instead of
  // patching rows: the moved subtree may be large, the payload carries full
  // rows (sort_key, is_system, …), and one RTT reuses the canonical listing.
  useEvent('ws:documents_moved_in', useCallback(() => {
    if (isPublicShare || !projectId) return;
    apiClient.get(`/projects/${projectId}`).then((data) => {
      setDocuments(data.documents);
    }).catch((err) => {
      console.error('Failed to refetch tree after documents moved in', err);
      useAppStore.getState().showToast(t('failedToLoadTree'), 'error');
    });
  }, [projectId, setDocuments, isPublicShare, t]));

  useEvent('open-create-doc', useCallback(({ parentId }: { parentId?: string }) => {
    // PUBLIC: no write affordances on /s/:token — and the authed POST /documents
    // would 401+toast on the anonymous surface.
    if (isPublicShare || !projectId || useAppStore.getState().accessLevel !== 'full') return;

    apiClient.post('/documents', {
      project_id: projectId,
      parent_id: parentId || null,
    }).then((newDoc) => {
      const freshDocs = useAppStore.getState().documents;
      if (!freshDocs.some(d => d.document_id === newDoc.document_id)) {
        setDocuments([...freshDocs, newDoc]);
      }
      emit('navigate-to-document', { documentId: newDoc.document_id });
      // Auto-rename: give breadcrumb time to render the new title, then trigger rename mode
      setTimeout(() => emit('breadcrumb-start-rename'), 100);
    }).catch((err) => {
      console.error('Failed to create document', err);
      useAppStore.getState().showToast(t('createDocumentFailed'), 'error');
    });
  }, [projectId, setDocuments, isPublicShare]));

  const handleDeleteConfirm = async (deleteChildren = true) => {
    if (!deleteModal) return;
    if (useAppStore.getState().accessLevel !== 'full') return;

    const target = deleteModal;
    const prevDocuments = documents;
    setDeleteModal(null);

    if (target.type === 'document') {
      // Subtree mode removes the WHOLE descendant set optimistically; lift mode
      // (deleteChildren=false) mirrors the backend lift: the target's children
      // are reparented to the target's parent in the same optimistic step. Why:
      // keeping their parent_id pointed at the removed row orphans them in the
      // tree builder and they vanish from the tree until a reload (live-drive
      // finding 2026-08-24).
      const deleted = prevDocuments.find(d => d.document_id === target.id);
      const idSet = deleteChildren && target.subtree
        ? new Set(target.subtree.ids)
        : new Set([target.id]);
      const remaining = deleteChildren
        ? prevDocuments.filter(d => !idSet.has(d.document_id))
        : prevDocuments
          .filter(d => d.document_id !== target.id)
          .map(d => (d.parent_id === target.id ? { ...d, parent_id: deleted?.parent_id ?? null } : d));

      // Optimistic UI: remove from tree, navigate away, notify peers — BEFORE the DELETE.
      // WHY: do NOT clear UIStore docState here. It is not user-visible and would
      // be permanently lost on rollback; defer the cleanup to the success branch.  Why: docState isn't user-visible; clearing it optimistically then rolling back a failed DELETE would permanently lose it, so cleanup defers to the success branch.
      setDocuments(remaining);
      emit('document-deleted', { documentId: target.id });
      if (documentId && idSet.has(documentId)) {
        // The open doc is being deleted: navigate to the nearest SURVIVING
        // ancestor of the deleted target (the parent itself may be in the set),
        // else the index doc, else the project root.
        let ancestorId: string | null = deleted?.parent_id ?? null;
        while (ancestorId && idSet.has(ancestorId)) {
          ancestorId = prevDocuments.find(d => d.document_id === ancestorId)?.parent_id ?? null;
        }
        const dest = (ancestorId && remaining.find(d => d.document_id === ancestorId))
          ?? remaining.find(d => d.is_index);
        if (dest) {
          emit('navigate-to-document', { documentId: dest.document_id });
        } else if (projectId) {
          navigate(`/projects/${projectId}`);
        }
      }

      apiClient.delete(`/documents/${target.id}?delete_children=${deleteChildren}`).then(() => {
        // Backend confirmed deletion — safe to drop UI state now.
        useUIStore.getState().removeDocStates([...idSet]);
      }).catch((err) => {
        console.error('Failed to delete document', err);
        setDocuments(prevDocuments);
        useAppStore.getState().showToast(t('failedToDeleteDocument'), 'error');
      });
    } else if (target.type === 'project') {
      navigate('/');
      apiClient.delete(`/projects/${target.id}`).catch((err) => {
        console.error('Failed to delete project', err);
        useAppStore.getState().showToast(t('failedToDeleteProject'), 'error');
      });
    }
  };

  return (
    <div className="h-full flex flex-col bg-surface">
      <DocumentTree
        onDelete={handleDelete}
        onCreateChild={handleCreateChild}
        onChangeParent={handleChangeParent}
        canEdit={canEdit}
      />

      {deleteModal && (
        <DeleteModal
          title={t('deleteType', { itemType: t(deleteModal.type) })}
          itemName={deleteModal.name}
          itemType={deleteModal.type}
          subtreeOption={deleteModal.subtree
            ? { childDocs: deleteModal.subtree.childDocs, childRefs: deleteModal.subtree.childRefs }
            : undefined}
          onConfirm={handleDeleteConfirm}
          onCancel={() => setDeleteModal(null)}
        />
      )}

      {parentPicker && (
        <ParentPickerPopup
          doc={parentPicker.doc}
          anchorRect={parentPicker.anchorRect}
          onClose={() => setParentPicker(null)}
        />
      )}
    </div>
  );
}
