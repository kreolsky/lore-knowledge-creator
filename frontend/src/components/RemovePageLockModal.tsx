/** PIN-confirmed removal of a personal page lock (SYSTEM: page-lock). Store: currentUser.locked_doc_ids. */

import { useState, useRef } from 'react';
import { apiClient, ForbiddenError, HttpError } from '../api/client';
import { useAppStore } from '../store/app-store';
import { Button, Modal, PinDigitsInput } from './ui';
import { useTranslation } from '../i18n';

export function RemovePageLockModal({ docId, onClose }: { docId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleRemove = async () => {
    if (pin.length !== 4) return;
    setLoading(true);
    setError('');
    try {
      const data: { locked_doc_ids: string[] } = await apiClient.post(`/cabinet/locked-docs/${docId}/remove`, { pin });
      useAppStore.setState(s => ({ currentUser: s.currentUser && { ...s.currentUser, locked_doc_ids: data.locked_doc_ids } }));
      onClose();
    } catch (err) {
      // WHY: the server answers a wrong PIN with 403 and an exhausted pin: budget with 429.
      const message = err instanceof ForbiddenError ? t('wrongPin')
        : err instanceof HttpError && err.status === 429 ? t('tooManyAttempts')
        : t('connectionError');
      setError(message);
      setPin('');
      inputRef.current?.focus();
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={t('unlockPagePermanently')}
      focusRef={inputRef}
      footer={(
        <>
          <Button variant="ghost" onClick={onClose}>{t('cancel')}</Button>
          <Button variant="primary" onClick={handleRemove} disabled={loading || pin.length !== 4}>
            {loading ? t('checking') : t('unlockPagePermanently')}
          </Button>
        </>
      )}
    >
      <div className="flex flex-col items-center gap-3">
        <p className="text-ui-base text-text-dim m-0 text-center">{t('removePageLockHint')}</p>
        <PinDigitsInput
          value={pin}
          onChange={v => { setPin(v); setError(''); }}
          onSubmit={handleRemove}
          ariaLabel={t('removePageLockHint')}
          inputRef={inputRef}
        />
        {error && <p className="text-ui-sm text-red m-0">{error}</p>}
      </div>
    </Modal>
  );
}
