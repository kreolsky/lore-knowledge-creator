/** Delete a reference's uploaded FILE while keeping the text — optimistic store
 * patch + rollback, shared by every media surface via MediaFileActions. The
 * header banner no longer offers this (it acts on the text only). A reference
 * with no text is deleted whole by the server (`reference_deleted`); the viewer
 * then moves on to the next image round the circle in the direction the user
 * last browsed, or closes when none is left.
 */

import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { useTranslation } from '../i18n';
import { imageAfterDelete } from './useImageReferenceNav';
import type { Reference } from '../types';

// Successor picked at click time, keyed by the reference being deleted. Read by
// whichever lands first: the collab `doc_deleted` (the server emits it BEFORE it
// answers the DELETE) or the DELETE response itself.
const nextAfterDelete = new Map<string, Reference | null>();

/** Leave a deleted reference that is open in the viewer: to the image picked when
 * this client deleted it (if it still exists), otherwise back to the document.
 * No-op when another item is open by now. */
export function closeDeletedReference(id: string): void {
  const state = useAppStore.getState();
  const next = nextAfterDelete.get(id);
  nextAfterDelete.delete(id);
  if (state.currentReference?.reference_id !== id) return;
  const stillThere = next && state.references.find(r => r.reference_id === next.reference_id);
  state.setCurrentReference(stillThere ?? null);
}

export function useReferenceFileDelete() {
  const { t } = useTranslation();
  const updateReference = useAppStore(s => s.updateReference);
  const setCurrentReference = useAppStore(s => s.setCurrentReference);

  // Plain handler (t used directly — never a dep array, see i18n rule). The
  // reference comes from PROPS (deferred layout commit), so currentReference is
  // only patched when this ref is still the open one.
  const deleteFile = async (reference: Reference) => {
    const id = reference.reference_id;
    const patch = { media_type: 'markdown' as const, file_path: null, file_meta: null, processing_status: null };
    const prevRef = { ...reference };
    // Picked before the optimistic patch: once patched to markdown the ref has
    // left the image list and has no position to step from.
    nextAfterDelete.set(id, imageAfterDelete(useAppStore.getState().references, id));
    updateReference(id, patch);
    if (useAppStore.getState().currentReference?.reference_id === id) {
      setCurrentReference({ ...reference, ...patch });
    }
    try {
      const res = await apiClient.delete(`/references/${id}/file`) as { reference_deleted: boolean };
      if (res.reference_deleted) {
        closeDeletedReference(id);
        useAppStore.getState().removeReference(id);
      } else {
        nextAfterDelete.delete(id);
      }
    } catch {
      nextAfterDelete.delete(id);
      updateReference(id, { media_type: prevRef.media_type, file_path: prevRef.file_path, file_meta: prevRef.file_meta, processing_status: prevRef.processing_status });
      if (useAppStore.getState().currentReference?.reference_id === id) {
        setCurrentReference(prevRef);
      }
      // No silent degradation: the rollback must be visible, not just applied.
      useAppStore.getState().showToast(t('failedToDeleteReferenceFile'), 'error');
    }
  };

  return { deleteFile };
}
