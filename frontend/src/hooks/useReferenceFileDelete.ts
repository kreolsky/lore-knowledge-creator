/** Delete a reference's uploaded FILE while keeping the text — optimistic store
 * patch + rollback, shared by every media surface via MediaFileActions. The
 * header banner no longer offers this (it acts on the text only).
 */

import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { useTranslation } from '../i18n';
import type { Reference } from '../types';

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
    updateReference(id, patch);
    if (useAppStore.getState().currentReference?.reference_id === id) {
      setCurrentReference({ ...reference, ...patch });
    }
    try {
      await apiClient.delete(`/references/${id}/file`);
    } catch {
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
