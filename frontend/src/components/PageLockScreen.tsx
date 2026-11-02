/** In-page PIN prompt shown instead of the editor for a page the user PIN-locked, or one under it (SYSTEM: page-lock). Store: addUnlockedDocId. */

import { useState, useRef, useEffect } from 'react';
import { Lock } from 'lucide-react';
import { apiClient, HttpError } from '../api/client';
import { useAppStore } from '../store/app-store';
import { Button, PinDigitsInput } from './ui';
import { useTranslation } from '../i18n';

export function PageLockScreen({ docId }: { docId: string }) {
  const addUnlockedDocId = useAppStore(s => s.addUnlockedDocId);
  const { t } = useTranslation();
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, [docId]);

  const handleUnlock = async () => {
    if (pin.length !== 4) return;
    setLoading(true);
    setError('');
    try {
      // WHY: the account PIN through the app lock's own verify-pin, so its rate limit applies.
      const data = await apiClient.post('/cabinet/verify-pin', { pin });
      if (data.valid) {
        addUnlockedDocId(docId);
        return;
      }
      setError(t('wrongPin'));
      setPin('');
      inputRef.current?.focus();
    } catch (err) {
      setError(err instanceof HttpError && err.status === 429 ? t('tooManyAttempts') : t('connectionError'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col items-center justify-center gap-6 bg-bg" data-page-lock-screen>
      <Lock size={32} className="text-text-dim" />
      <div className="text-center">
        <h2 className="text-lg font-bold m-0 mb-1 text-text">{t('pageLockedTitle')}</h2>
        <p className="text-ui-base text-text-dim m-0">{t('enterPinToOpenPage')}</p>
      </div>
      <div className="flex flex-col items-center gap-3">
        <PinDigitsInput
          value={pin}
          onChange={v => { setPin(v); setError(''); }}
          onSubmit={handleUnlock}
          ariaLabel={t('enterPinToOpenPage')}
          inputRef={inputRef}
        />
        {error && <p className="text-ui-sm text-red m-0">{error}</p>}
        <Button variant="primary" onClick={handleUnlock} disabled={loading || pin.length !== 4}>
          {loading ? t('checking') : t('unlock')}
        </Button>
      </div>
    </div>
  );
}
