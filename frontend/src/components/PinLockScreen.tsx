/** Full-viewport PIN lock overlay — blocks all interaction until PIN or logout. Store slices: setCurrentUser, setPinLocked. */

import { useState, useRef, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiClient } from '../api/client';
import { useAppStore } from '../store/app-store';
import { Button, PinDigitsInput } from './ui';
import { useTranslation } from '../i18n';
import { resetEditorHost } from './editor/editor-host';

export function PinLockScreen() {
  const navigate = useNavigate();
  const setCurrentUser = useAppStore(s => s.setCurrentUser);
  const setPinLocked = useAppStore(s => s.setPinLocked);
  const { t } = useTranslation();

  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleUnlock = async () => {
    if (pin.length !== 4) return;
    setLoading(true);
    setError('');
    try {
      const res = await fetch('/api/cabinet/verify-pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ pin }),
      });
      const data = await res.json();
      if (data.valid) {
        setPinLocked(false);
      } else {
        setError(t('wrongPin'));
        setPin('');
        inputRef.current?.focus();
      }
    } catch {
      setError(t('connectionError'));
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = async () => {
    try {
      await apiClient.post('/auth/logout', {});
    } catch {
      // Ignore
    }
    resetEditorHost({ scope: 'user' });
    // setCurrentUser(null) keeps its internal clearLastSavedBlobs() as an idempotent
    // safety net for direct callers (e.g. app-store.test) that bypass this logout path.
    setCurrentUser(null);
    navigate('/');
  };

  return (
    <div className="fixed inset-0 z-[9999] bg-bg flex flex-col items-center justify-center gap-6">
      {/* Logo */}
      <div className="w-12 h-12 bg-accent text-white flex items-center justify-center text-ui-xl font-bold">
        L
      </div>

      <div className="text-center">
        <h2 className="text-lg font-bold m-0 mb-1 text-text">
          {t('locked')}
        </h2>
        <p className="text-ui-base text-text-dim m-0">
          {t('enterPinToUnlock')}
        </p>
      </div>

      <div className="flex flex-col items-center gap-3">
        <PinDigitsInput
          value={pin}
          onChange={v => { setPin(v); setError(''); }}
          onSubmit={handleUnlock}
          ariaLabel={t('enterPinToUnlock')}
          inputRef={inputRef}
        />

        {error && (
          <p className="text-ui-sm text-red m-0">{error}</p>
        )}

        <Button
          variant="primary"
          onClick={handleUnlock}
          disabled={loading || pin.length !== 4}
        >
          {loading ? t('checking') : t('unlock')}
        </Button>

        <Button
          variant="ghost"
          onClick={handleLogout}
        >
          {t('signOutInstead')}
        </Button>
      </div>
    </div>
  );
}
