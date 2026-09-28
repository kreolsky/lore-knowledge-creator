/** Banner shown when previewing a document checkpoint snapshot. Mirrors ReferenceViewerBanner styling/structure: filled plate (no visible border), ghost icon-buttons aligned right. */

import { useState } from 'react';
import { History, ArrowLeft } from 'lucide-react';
import { Button, Modal } from '../ui';
import { useAppStore } from '../../store/app-store';
import { apiClient, HttpError } from '../../api/client';
import { useTranslation } from '../../i18n';

export function SnapshotPreviewBanner() {
  const { t } = useTranslation();
  const snapshotPreview = useAppStore(s => s.snapshotPreview);
  const setSnapshotPreview = useAppStore(s => s.setSnapshotPreview);
  const setCurrentDocument = useAppStore(s => s.setCurrentDocument);
  const showToast = useAppStore(s => s.showToast);
  const currentDocument = useAppStore(s => s.currentDocument);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [isRestoring, setIsRestoring] = useState(false);

  if (!snapshotPreview || !currentDocument) return null;

  const handleBack = () => {
    setSnapshotPreview(null);
    const docId = useAppStore.getState().currentDocument?.document_id;
    if (!docId) return;
    apiClient.get(`/documents/${docId}`).then(data => {
      const store = useAppStore.getState();
      if (store.currentDocument?.document_id !== docId) return;
      useAppStore.setState(prev => ({
        currentDocument: prev.currentDocument
          ? {
              ...prev.currentDocument,
              headings: data.headings,
              updated_at: data.updated_at,
            }
          : null,
      }));
    }).catch(() => showToast(t('metadataRefreshFailed'), 'error'));
  };

  // ARCH: Snapshot restore is triggered from the preview banner only; HistoryPanel does not own the action.
  // Why: restoring only makes sense after the user has actually viewed the snapshot — keep the trigger next to the preview.
  const handleRestoreSubmit = async () => {
    if (isRestoring) return;
    setIsRestoring(true);
    try {
      await apiClient.post(`/checkpoints/${snapshotPreview.checkpoint_id}/restore`, {});
      const data = await apiClient.get(`/documents/${currentDocument.document_id}`);
      setCurrentDocument(data);
      setSnapshotPreview(null);
      setConfirmOpen(false);
    } catch (err) {
      // INVARIANT: surface restore failures to the user — never swallow (no silent  Why: restore failures must surface (no silent degradation); 409=corrupted (doc untouched), 503=blob transiently unreadable — distinct so the user gets the right message.
      // degradation). 409 = corrupted (aborted, document untouched). 503 = blob
      // transiently unreadable (also aborted) — distinct from corruption so the user
      // gets the right diagnosis. Anything else is a generic restore failure.
      if (err instanceof HttpError && err.status === 409) {
        showToast(t('restoreCorrupted'), 'error');
      } else if (err instanceof HttpError && err.status === 503) {
        showToast(t('checkpointTemporarilyUnavailable'), 'error');
      } else {
        showToast(t('restoreFailed'), 'error');
      }
    } finally {
      setIsRestoring(false);
    }
  };

  return (
    <>
      <div className="snapshot-preview-banner snapshot-viewer-banner">
        <div className="flex gap-1.5 items-center ml-auto">
          <Button variant="ghost" size="sm" onClick={() => setConfirmOpen(true)}>
            <History size={13} /> {t('restore')}
          </Button>
          <Button variant="ghost" size="sm" onClick={handleBack}>
            <ArrowLeft size={13} /> {currentDocument.title}
          </Button>
        </div>
      </div>

      <Modal
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t('restoreSnapshot')}
        width={360}
        closeLabel={t('close')}
        footer={<>
          <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={isRestoring}>{t('cancel')}</Button>
          <Button variant="danger" onClick={handleRestoreSubmit} disabled={isRestoring}>{isRestoring ? t('restoring') : t('restore')}</Button>
        </>}
      >
        <div className="text-ui-base text-text-muted leading-relaxed">
          {t('restoreWarning')}
        </div>
      </Modal>
    </>
  );
}
