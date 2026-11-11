/** Shared clipboard copy with toast feedback + error handling (no silent
 * degradation). Replaces the duplicated copy logic in AI and notes wrappers. */
// SYSTEM: chat-composer — shared clipboard copy helper

import { t } from '../../../i18n';

type ShowToast = (message: string, type?: 'info' | 'error' | 'warning', options?: { persistent?: boolean }) => void;

/** Copies `text` to the clipboard and shows a toast on success or failure.
 *  `onSuccess` (optional) runs only after a confirmed write (e.g. swap a copy→check icon).
 *  `onFailure` (optional) replaces the default failure toast — for a value the user
 *  must still be able to reach when the write did not happen (e.g. an invite link). */
export function copyWithToast(
  text: string,
  showToast: ShowToast,
  onSuccess?: () => void,
  onFailure?: () => void,
) {
  const fail = onFailure ?? (() => showToast(t('copyFailed'), 'error'));
  // No silent degradation: on a non-secure origin (plain http, non-localhost)
  // navigator.clipboard does not EXIST, and the property access would throw
  // synchronously — before the .catch attaches, killing the click handler
  // with no feedback. Degrade explicitly to the failure toast instead.
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
  if (!clipboard?.writeText) {
    fail();
    return;
  }
  clipboard.writeText(text)
    .then(() => {
      showToast(t('copied'), 'info');
      onSuccess?.();
    })
    .catch(fail);
}
