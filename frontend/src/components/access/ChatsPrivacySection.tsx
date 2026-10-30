/**
 * Subtree chat privacy — the Access-tab checkboxes for `documents.chats_unindexed`
 * (search hide) and `documents.chats_unlisted` (list hide). Storage-wise two
 * independent columns (each governs one surface), but the UI GATES them like the
 * General-access Share/Include-subtree pair (operator 2026-10-30): the list hide
 * is meaningful only layered on the search hide, so its checkbox stays disabled
 * until the search one is on, and turning the search hide off takes the list
 * hide with it (one PATCH, both fields — an "unlisted but findable" row cannot
 * appear from this tab).
 *
 * Each flag is read from the project's flat `documents` list (the same row the
 * doc-tree mark reads), so a checkbox and its mark never disagree. A flip is
 * optimistic: only the flipped flag's field of that row changes, then the PATCH
 * response's value is written back; on failure the prior value returns and a
 * toast says why — no silent degradation.
 */
import { ShieldCheck } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { FieldCheckbox, SectionHeader } from '../ui';
import { useTranslation } from '../../i18n';
import type { Document } from '../../types';

type ChatsFlagField = 'chats_unindexed' | 'chats_unlisted';

// WHY field-level merge (never replace the row with the PATCH response): the tree
// row carries server-derived fields only GET /api/projects/{id} computes
// (key_capabilities, public_share); a whole-row replace would drop the key/share
// icon until reload.
function setFlag(docId: string, field: ChatsFlagField, value: boolean) {
  const { documents, setDocuments } = useAppStore.getState();
  setDocuments(documents.map(d => (d.document_id === docId ? { ...d, [field]: value } : d)));
}

export function ChatsPrivacySection({ docId }: { docId: string }) {
  const { t } = useTranslation();
  const checkedUnindexed = useAppStore(s => !!s.documents.find(d => d.document_id === docId)?.chats_unindexed);
  const checkedUnlisted = useAppStore(s => !!s.documents.find(d => d.document_id === docId)?.chats_unlisted);

  const flip = async (field: ChatsFlagField, next: boolean) => {
    if (useAppStore.getState().accessLevel !== 'full') return;
    // INVARIANT: the list hide never outlives the search hide from this tab.
    // Why: the Share/Include-subtree gate pattern (operator 2026-10-30, "как для
    // документа и поддерева"); and the clear is ONE PATCH, not two — two steps
    // would flash an "unlisted but findable" row between requests and could
    // half-land on failure.
    const patch: Partial<Record<ChatsFlagField, boolean>> = { [field]: next };
    if (field === 'chats_unindexed' && !next
      && !!useAppStore.getState().documents.find(d => d.document_id === docId)?.chats_unlisted) {
      patch.chats_unlisted = false;
    }
    for (const [f, v] of Object.entries(patch)) setFlag(docId, f as ChatsFlagField, v as boolean);
    try {
      const updated: Document = await apiClient.patch(`/documents/${docId}`, patch);
      for (const f of Object.keys(patch) as ChatsFlagField[]) setFlag(docId, f, !!updated[f]);
    } catch (err) {
      console.error('Failed to change subtree chat visibility', err);
      for (const [f, v] of Object.entries(patch)) setFlag(docId, f as ChatsFlagField, !v as boolean);
      useAppStore.getState().showToast(
        t(field === 'chats_unlisted' ? 'docChatsUnlistedFailed' : 'docChatsUnindexedFailed'),
        'error',
      );
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
            checked={checkedUnindexed}
            onChange={(v) => { void flip('chats_unindexed', v); }}
            label={t('accessChatsPrivateToggle')}
          />
          <FieldCheckbox
            checked={checkedUnlisted}
            disabled={!checkedUnindexed}
            onChange={(v) => { void flip('chats_unlisted', v); }}
            label={t('accessChatsUnlistedToggle')}
            title={t('accessChatsUnlistedGatedHint')}
          />
        </div>
      </div>
    </>
  );
}
