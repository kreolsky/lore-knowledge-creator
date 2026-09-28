/**
 * Subscribes to the `voice-transcription-error` event and surfaces a user-visible toast.
 *
 * // ARCH: useVoiceInput removes the voice widget on failure and emits this event, but it
 * //      must not own the toast (it has no React/i18n context). This hook is the single
 * //      consumer that turns a silent widget-vanish into detected user feedback, per the
 * //      project's no-silent-degradation rule.
 */
// SYSTEM: voice-input — error feedback for failed voice transcription

import { useEvent } from './useEvent';
import { useAppStore } from '../store/app-store';
import { useTranslation } from '../i18n';

export function useVoiceTranscriptionError(): void {
  const { t } = useTranslation();
  useEvent('voice-transcription-error', () => {
    useAppStore.getState().showToast(t('voiceTranscriptionFailed'), 'error');
  });
}
