/** Optimistic update helper — apply immediately, confirm on success, rollback on error.
 *
 * WHY: rename inputs close before the API returns, so the UI briefly shows the old
 * name. Apply the new value first; on success replace with the authoritative server
 * response; on failure restore the original and tell the user the change was not saved.
 */
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';

export async function withOptimistic<T>(
  optimisticVal: T,
  rollbackVal: T,
  setter: (v: T) => void,
  apiFn: () => Promise<T>,
): Promise<void> {
  setter(optimisticVal);
  try {
    setter(await apiFn());
  } catch (err) {
    console.error('Optimistic update failed, rolling back', err);
    setter(rollbackVal);
    useAppStore.getState().showToast(t('changeNotSaved'), 'error');
  }
}
