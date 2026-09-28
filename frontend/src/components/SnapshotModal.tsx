/** Modal for creating a named checkpoint (snapshot) of the current document content. Store slices: currentDocument, snapshotModalOpen, snapshotPendingContent, closeSnapshotModal. Events emitted: snapshot-created. */

import { useState } from 'react';
import { Button, Modal, FieldTextarea } from './ui';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import { useShallow } from 'zustand/react/shallow';
import { apiClient } from '../api/client';
import { emit } from '../events';
import { Checkpoint } from '../types';

export function SnapshotModal() {
  const { currentDocument, snapshotModalOpen, snapshotPendingContent, snapshotPendingTablesJson, closeSnapshotModal, showToast } = useAppStore(useShallow(s => ({ currentDocument: s.currentDocument, snapshotModalOpen: s.snapshotModalOpen, snapshotPendingContent: s.snapshotPendingContent, snapshotPendingTablesJson: s.snapshotPendingTablesJson, closeSnapshotModal: s.closeSnapshotModal, showToast: s.showToast })));
  const { t } = useTranslation();
  const [comment, setComment] = useState('');
  const [isLoading, setIsLoading] = useState(false);

  const handleClose = () => {
    closeSnapshotModal();
    setComment('');
  };

  const handleSubmit = async () => {
    if (!currentDocument || isLoading) return;
    // WHY: snapshotPendingContent/TablesJson are captured at hotkey time
    // (the editor freezes them when the modal opens), so the snapshot records the
    // user's intent at Cmd+S, not whatever async flush lands during the dialog.  Why: the editor freezes content/tables at hotkey time so the snapshot records Cmd+S intent, not whatever async flush lands during the modal.
    setIsLoading(true);
    try {
      const newSnap = await apiClient.post('/checkpoints', {
        document_id: currentDocument.document_id,
        content: snapshotPendingContent ?? '',
        tables_json: snapshotPendingTablesJson ?? null,
        comment,
      }) as Checkpoint;
      handleClose();
      emit('snapshot-created', newSnap);
    } catch (err) {
      // No silent degradation: a failed snapshot must surface to the user, not just
      // the console (project rule — never swallow user-facing op errors).
      console.error('Failed to create snapshot', err);
      showToast(t('snapshotCreateFailed'), 'error');
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Modal open={snapshotModalOpen} onClose={handleClose} title={t('createSnapshot')} closeLabel={t('close')}
      footer={<>
        <Button variant="ghost" onClick={handleClose} disabled={isLoading}>{t('cancel')}</Button>
        <Button variant="primary" onClick={handleSubmit} disabled={isLoading}>{isLoading ? t('saving') : t('saveSnapshotAction')}</Button>
      </>}
    >
      <div>
        <div className="field-label">{t('commentOptional')}</div>
        <FieldTextarea
          value={comment}
          onChange={e => setComment(e.target.value)}
          placeholder={t('whatChanged')}
          autoFocus
          onKeyDown={e => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault();
              handleSubmit();
            }
            if (e.key === 'Escape') handleClose();
          }}
        />
      </div>
    </Modal>
  );
}
