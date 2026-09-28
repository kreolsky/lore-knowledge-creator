/** Shared clipboard copy with toast feedback + error handling (no silent
 * degradation). Replaces the duplicated copy logic in AI and notes wrappers. */
// SYSTEM: chat-composer — shared clipboard copy helper

import { t } from '../../../i18n';

type ShowToast = (message: string, type?: 'info' | 'error' | 'warning', options?: { persistent?: boolean }) => void;

/** Copies `text` to the clipboard and shows a toast on success or failure.
 *  `onSuccess` (optional) runs only after a confirmed write (e.g. swap a copy→check icon). */
export function copyWithToast(
  text: string,
  showToast: ShowToast,
  onSuccess?: () => void,
) {
  // No silent degradation: on a non-secure origin (plain http, non-localhost)
  // navigator.clipboard does not EXIST, and the property access would throw
  // synchronously — before the .catch attaches, killing the click handler
  // with no feedback. Degrade explicitly to the failure toast instead.
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
  if (!clipboard?.writeText) {
    showToast(t('copyFailed'), 'error');
    return;
  }
  clipboard.writeText(text)
    .then(() => {
      showToast(t('copied'), 'info');
      onSuccess?.();
    })
    .catch(() => showToast(t('copyFailed'), 'error'));
}
