/**
 * Subtree chat privacy — the Access-tab checkbox for `documents.chats_unindexed`.
 *
 * The flag is read from the project's flat `documents` list (the same row the
 * doc-tree mark reads), so the checkbox and the mark never disagree. A flip is
 * optimistic: only the `chats_unindexed` field of that row changes, then the
 * PATCH response's value is written back; on failure the prior value returns
 * and a toast says why — no silent degradation.
 */
import { ShieldCheck } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { FieldCheckbox, SectionHeader } from '../ui';
import { useTranslation } from '../../i18n';
import type { Document } from '../../types';

// WHY field-level merge (never replace the row with the PATCH response): the tree
// row carries server-derived fields only GET /api/projects/{id} computes
// (key_capabilities, public_share); a whole-row replace would drop the key/share
// icon until reload.
function setFlag(docId: string, value: boolean) {
  const { documents, setDocuments } = useAppStore.getState();
  setDocuments(documents.map(d => (d.document_id === docId ? { ...d, chats_unindexed: value } : d)));
}

export function ChatsPrivacySection({ docId }: { docId: string }) {
  const { t } = useTranslation();
  const checked = useAppStore(s => !!s.documents.find(d => d.document_id === docId)?.chats_unindexed);

  const flip = async (next: boolean) => {
    if (useAppStore.getState().accessLevel !== 'full') return;
    setFlag(docId, next);
    try {
      const updated: Document = await apiClient.patch(`/documents/${docId}`, { chats_unindexed: next });
      setFlag(docId, !!updated.chats_unindexed);
    } catch (err) {
      console.error('Failed to change subtree chat visibility', err);
      setFlag(docId, !next);
      useAppStore.getState().showToast(t('docChatsUnindexedFailed'), 'error');
    }
  };

  return (
    <>
      <SectionHeader
        icon={<ShieldCheck size={11} className="text-green" />}
        title={t('accessChatsPrivateTitle')}
        description={t('accessChatsPrivateDesc')}
      />
      <div className="px-3.5">
        <div className="flex flex-col gap-1.5 mt-1">
          <FieldCheckbox
            checked={checked}
            onChange={(v) => { void flip(v); }}
            label={t('accessChatsPrivateToggle')}
          />
        </div>
      </div>
    </>
  );
}
