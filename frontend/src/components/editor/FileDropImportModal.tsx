/** Confirmation modal for the editor file-drop import (see useEditorFileDrop). */

import { useRef } from 'react';
import { Button, Modal } from '../ui';
import { useTranslation } from '../../i18n';
import type { FileDropModalProps } from './useEditorFileDrop';

export function FileDropImportModal({ pendingDrop, importing, onCancel, onConfirm }: FileDropModalProps) {
  const { t } = useTranslation();
  // Ref to the Insert button so the confirm modal focuses the primary action on
  // open instead of the default header close (X).
  const confirmBtnRef = useRef<HTMLButtonElement | null>(null);

  return (
    <Modal
      open={!!pendingDrop}
      onClose={() => { if (!importing) onCancel(); }}
      title={pendingDrop?.file.name ?? ''}
      width={420}
      closeLabel={t('close')}
      focusRef={confirmBtnRef}
      footer={<>
        <Button variant="ghost" onClick={onCancel} disabled={importing}>
          {t('cancel')}
        </Button>
        <Button ref={confirmBtnRef} variant="primary" onClick={onConfirm} disabled={importing}>
          {importing ? t('importingFile') : t('insert')}
        </Button>
      </>}
    >
      <div className="text-ui-base text-text-muted leading-[1.6]">
        {t('insertFileBelowCursorConfirm')}
      </div>
    </Modal>
  );
}
