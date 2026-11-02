/**
 * Personal page PIN lock — the first checkbox of the Access tab's privacy section
 * (SYSTEM: page-lock). Checking locks the page for THIS user (optimistic, rollback +
 * toast); unchecking opens the PIN-confirmed remove modal. Disabled until the user has
 * an account PIN. Store: currentUser.locked_doc_ids/has_pin, addUnlockedDocId.
 */
import { useState } from 'react';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { FieldCheckbox } from '../ui';
import { useTranslation } from '../../i18n';
import { RemovePageLockModal } from '../RemovePageLockModal';

function setLockedIds(ids: string[]) {
  useAppStore.setState(s => ({ currentUser: s.currentUser && { ...s.currentUser, locked_doc_ids: ids } }));
}

export function PageLockToggle({ docId }: { docId: string }) {
  const { t } = useTranslation();
  const locked = useAppStore(s => !!s.currentUser?.locked_doc_ids?.includes(docId));
  const hasPin = useAppStore(s => !!s.currentUser?.has_pin);
  const [removeOpen, setRemoveOpen] = useState(false);

  const lock = async () => {
    const store = useAppStore.getState();
    if (!store.currentUser?.has_pin) return;
    const prev = store.currentUser.locked_doc_ids ?? [];
    // WHY: the tab acts on the OPEN page — locking it keeps it open for this visit; it locks on leave.
    store.addUnlockedDocId(docId);
    setLockedIds([...prev, docId]);
    try {
      const data: { locked_doc_ids: string[] } = await apiClient.post(`/cabinet/locked-docs/${docId}`, {});
      setLockedIds(data.locked_doc_ids);
    } catch (err) {
      console.error('Failed to lock document', err);
      setLockedIds(prev);
      store.showToast(t('lockPageFailed'), 'error');
    }
  };

  return (
    <>
      <FieldCheckbox
        checked={locked}
        disabled={!locked && !hasPin}
        onChange={(v) => { if (v) void lock(); else setRemoveOpen(true); }}
        label={t('accessPageLockToggle')}
        title={!locked && !hasPin ? t('lockPageNeedsPin') : undefined}
      />
      {removeOpen && <RemovePageLockModal docId={docId} onClose={() => setRemoveOpen(false)} />}
    </>
  );
}
