/** Reference CRUD event listeners — always active regardless of panel state. */
// ARCH: Lives here (not in ReferencesPanel) because ReferencesPanel is conditionally rendered.
// If the panel is closed, listeners don't exist, and collab-driven store updates are missed.

import { useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { useChatStore } from '../store/chat-store';
import { useDeletedRefIds } from '../store/deleted-ref-ids';
import { apiClient } from '../api/client';
import { useEvent } from './useEvent';
import { useTranslation } from '../i18n';
import { invalidateRefPreview } from './useReferencePreview';
import { projectRefIds, missingRefIds } from '../components/editor/live-preview/link-validity';
import { emit } from '../events';
import type { Reference } from '../types';

function _cleanupReferenceDeletion(referenceId: string): void {
  useAppStore.getState().removeReference(referenceId);
  // Both delete events (single and batch) land here, so the chat's image plates
  // learn every delete, whichever surface or tab made it.
  useDeletedRefIds.getState().add([referenceId]);
  // INVARIANT: evict the preview cache on delete.
  // Why: it is the shared content source for hover previews + transclusions, so a
  // surviving entry would re-serve a stale body for a deleted reference.
  invalidateRefPreview(referenceId);
  // Backend confirmed deletion — safe to drop from deletedRefIds. Any subsequent
  // fetch of /references will not include this ID (deleted_at IS NOT NONE).
  useAppStore.getState().removeRefFromDeleting(referenceId);
  if (useAppStore.getState().currentReference?.reference_id === referenceId) {
    useChatStore.getState().reset();
  }
}

export function useReferenceEvents() {
  const addReference = useAppStore(s => s.addReference);
  const updateReference = useAppStore(s => s.updateReference);
  const setCurrentReference = useAppStore(s => s.setCurrentReference);
  const { t } = useTranslation();

  const fetchRef = useCallback(async (referenceId: string) => {
    try {
      const ref = await apiClient.get(`/references/${referenceId}`) as Reference;
      return ref;
    } catch (err) {
      console.error(`Failed to fetch reference ${referenceId}:`, err);
      useAppStore.getState().showToast(t('failedToFetchReference'), 'error');
      return null;
    }
  }, [t]);

  // WHY: a newly-created reference (voice note, worker job, external system) is added
  // to the panel even when its parent is a different/unrelated document — shown with that
  // parent's label. Why: same stale-until-reload feature; lets the user see and open a
  // just-created reference in context. Do NOT gate addReference on current-doc scope.
  useEvent('ws:reference_created', useCallback(async ({ reference_id: referenceId }) => {
    // see SYSTEM: transclusion — the ref: resolver may have memoized this id as
    // missing; a creation flips that verdict. Drop it and ask the editor to
    // re-probe the open doc's text ('ref-links-invalidate' → resolve pass).
    missingRefIds.delete(referenceId);
    emit('ref-links-invalidate', { reference_id: referenceId });
    const state = useAppStore.getState();
    if (state.references.some(r => r.reference_id === referenceId)) return;
    if (state.pendingUploadRefIds.has(referenceId)) return;
    if (state.deletedRefIds.has(referenceId)) return;
    const ref = await fetchRef(referenceId);
    if (ref) addReference(ref);
  }, [addReference, fetchRef]));

  useEvent('ws:reference_renamed', useCallback(({ reference_id: referenceId, title }) => {
    updateReference(referenceId, { title });
  }, [updateReference]));

  // WHY: a moved reference stays in the References panel under its NEW parent's
  // label; we re-place it (document_id + sort_key) into the new group's run,
  // never remove it from the current view.
  // Why: deliberate "stale-until-reload" UX — the user can keep working with a just-moved
  // reference in place. The panel re-scopes to the current doc only on the next full load
  // (ReferencesPanel scoped fetch). Do NOT add scope-revalidation/removal here. placeReference
  // makes the ref JOIN its new group's run (leaving the old run contiguous) instead of
  // patching document_id in place and splitting the old run.
  useEvent('ws:reference_moved', useCallback(({ reference_id: referenceId, document_id: documentId, sort_key: sortKey }) => {
    useAppStore.getState().placeReference(referenceId, { document_id: documentId, sort_key: sortKey ?? undefined });
  }, []));

  // A reference REORDER rides the one `document_reordered` event (both kinds).
  // The tree handler in Sidebar maps by document
  // ids, so a ref id is a no-op there; here the id names a reference in this
  // client's list → re-place it in its group with the authoritative key.
  // Doc reorders never enter this branch (a doc id is not in `references`).
  useEvent('ws:document_reordered', useCallback(({ document_id: documentId, sort_key: sortKey }) => {
    const state = useAppStore.getState();
    if (sortKey && state.references.some(r => r.reference_id === documentId)) {
      state.placeReference(documentId, { sort_key: sortKey });
    }
  }, []));

  // A reference re-parented via move_document
  // arrives as a `ws:document_moved` bus event (the tree move), NOT a
  // ws:reference_moved event. The reference panel loads per-host, so a client
  // showing EITHER the new or the previous host must re-fetch its list — otherwise an
  // open panel keeps listing a file that left, or misses one that arrived (stale-as-
  // current). Each client shows one doc, so it only reloads when ITS current doc is a
  // host of the move. (Harmless for tree-document moves: the per-host reference list
  // is unaffected, so the re-fetch returns the same data.)
  useEvent('ws:document_moved', useCallback(({ parent_id: parentId, previous_parent_id: previousParentId }) => {
    const cur = useAppStore.getState().currentDocument?.document_id ?? null;
    if (cur && (cur === parentId || cur === previousParentId)) {
      useAppStore.getState().bumpReferencesReload();
    }
  }, []));

  useEvent('ws:reference_deleted', useCallback(({ reference_id: referenceId }) => {
    // The ref: resolver's verdict is now stale in the OTHER direction: drop the
    // id from both Sets (a re-created id re-probes) and ask the editor to re-run
    // the pass — the probe answers "absent", the link goes broken and the pass's
    // own-source delete drops any project-ref band entry.
    projectRefIds.delete(referenceId);
    missingRefIds.delete(referenceId);
    emit('ref-links-invalidate', { reference_id: referenceId });
    _cleanupReferenceDeletion(referenceId);
  }, []));

  useEvent('ws:documents_deleted_batch', useCallback(({ reference_ids: referenceIds }) => {
    // ARCH: reference cleanup for batch-delete lives here (always mounted), NOT in Sidebar
    // (conditionally rendered). Without this, batch-deleted references leave ghost entries
    // when the user is on a non-docs tab.
    for (const refId of referenceIds) {
      _cleanupReferenceDeletion(refId);
    }
  }, []));

  // Cross-project subtree move, SOURCE side: the references that traveled with
  // the subtree (leaves under moved docs) are unreachable from THIS project's
  // panels — the same cleanup as batch-delete (drop + preview evict + chat
  // reset when the moved ref was open). The ref is not deleted, it lives in
  // the target project now; this client's project-A context cannot serve it.
  useEvent('ws:documents_moved_out', useCallback(({ reference_ids: referenceIds }) => {
    for (const refId of referenceIds) {
      _cleanupReferenceDeletion(refId);
    }
  }, []));

  useEvent('ws:reference_updated', useCallback(async ({ reference_id: referenceId }) => {
    // WHY: on a content change, evict the cached body.
    // Why: the next hover preview / transclusion must re-fetch fresh content —
    // a surviving entry serves the pre-edit body.
    invalidateRefPreview(referenceId);
    const ref = await fetchRef(referenceId);
    if (!ref) return;
    // ARCH: the PATCH-archived route reuses this event.
    // Archive direction: the ref is already in this client's list → updateReference
    // re-affirms `archived` IN PLACE (dim-in-place, no reorder). The handler MUST NOT
    // auto-remove archived refs — that would kill dim-in-place on the acting client
    // (the WS echo flows through this same handler). Filtering happens at the next
    // server refetch (toggle OFF hides it, toggle ON sinks it).
    //
    // RESTORE direction (asymmetry fix): a client that already refetched-and-filtered
    // the ref OUT (toggle OFF, or a doc switch) hits an updateReference `.map` no-op
    // (id absent → nothing to update) → the restored ref would stay missing until that
    // client's next manual refetch. Patch that ONE gap: if the id is absent AND the ref
    // is no longer archived, re-LIST so the restored ref re-enters scope. Archive needs
    // no such fallback (the ref is still present to dim).
    const present = useAppStore.getState().references.some(r => r.reference_id === referenceId);
    if (!present && !ref.archived) {
      useAppStore.getState().bumpReferencesReload();
      return;
    }
    updateReference(referenceId, ref);
    if (useAppStore.getState().currentReference?.reference_id === referenceId) {
      setCurrentReference(ref);
    }
  }, [updateReference, setCurrentReference, fetchRef]));

  // WHY: every content flush evicts that id's cached reference body — not only
  // reference_updated, which the REST paths emit but a collab edit never does. The cache is the hover-preview source, so an edited reference kept showing the
  // body fetched when the tab opened. Unconditional on is_reference: not every emitter
  // states it, and evicting an id the cache does not hold is a no-op.
  useEvent('ws:content_flushed', useCallback(({ entity_id: entityId }) => {
    invalidateRefPreview(entityId);
  }, []));

  useEvent('ws:reference_status_changed',useCallback(async ({ reference_id: referenceId, status }) => {
    updateReference(referenceId, { processing_status: status } as Partial<Reference>);
    if (status === 'ready') {
      // WHY: when transcription/content lands, evict the cached body.
      // Why: the new content must be fetched on the next hover/transclusion —
      // a surviving entry serves the empty pre-transcription body.
      invalidateRefPreview(referenceId);
      const ref = await fetchRef(referenceId);
      if (!ref) return;
      updateReference(referenceId, ref);
      if (useAppStore.getState().currentReference?.reference_id === referenceId) {
        setCurrentReference(ref);
      }
    }
  }, [updateReference, setCurrentReference, fetchRef]));

  useEvent('ws:agent_extraction_started', useCallback(() => {
    useAppStore.getState().showToast(t('agentAutoStarted'), 'info');
  }, [t]));
}
