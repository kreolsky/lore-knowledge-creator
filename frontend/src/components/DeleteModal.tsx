/** Confirmation modal for destructive delete actions. Props-driven state; uses showToast for copy-to-clipboard feedback. */
// SYSTEM: delete-confirm-modal — type-name-to-confirm destructive delete dialog

import { useState } from 'react';
import { Button, Modal, FieldCheckbox, FieldInput } from './ui';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import type { TranslationKey } from '../i18n';

const NOM_KEY: Record<string, TranslationKey> = { document: 'documentNom', project: 'projectNom', note: 'noteNom' };
const PARTICIPLE_KEY: Record<string, TranslationKey> = { document: 'deletedM', project: 'deletedM', note: 'deletedF' };
const NAME_SENTINEL = '__NAME__';

interface DeleteModalProps {
  title: string;
  itemName: string;
  itemType: 'project' | 'document' | 'note';
  /** Present only for documents WITH live descendants (docs or hosted refs):
   * renders the "delete the whole subtree" checkbox, checked by default.
   * Absent ⇒ no checkbox — the flag is irrelevant (subtree = the item itself). */
  subtreeOption?: { childDocs: number; childRefs: number };
  onConfirm: (deleteChildren?: boolean) => void | Promise<void>;
  onCancel: () => void;
}

/** Render translated text with an interactive itemName span (code font, «quotes», click-to-copy). */
function InterpolatedName({ template, itemName }: { template: string; itemName: string }) {
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);
  const parts = template.split(NAME_SENTINEL);

  const handleCopy = () => {
    navigator.clipboard.writeText(itemName);
    showToast(t('copied'), 'info');
  };

  return (
    <>
      {parts[0]}
      <span
        className="font-mono cursor-pointer text-text hover:underline"
        title={t('copy')}
        onClick={handleCopy}
      >
        «{itemName}»
      </span>
      {parts[1]}
    </>
  );
}

export function DeleteModal({ title, itemName, itemType, subtreeOption, onConfirm, onCancel }: DeleteModalProps) {
  const [confirmText, setConfirmText] = useState('');
  // Default ON: deleting a node deletes its whole subtree — unchecking opts into
  // the legacy lift mode (children moved to the grandparent).
  const [deleteSubtree, setDeleteSubtree] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const { t } = useTranslation();
  const isMatch = confirmText === itemName;

  const warningText = t('deleteWarning', {
    itemType: t(NOM_KEY[itemType]),
    itemName: NAME_SENTINEL,
    participle: t(PARTICIPLE_KEY[itemType]),
  });

  const confirmLabel = t('typeToConfirm', { itemName: NAME_SENTINEL });

  const handleConfirm = async () => {
    if (!isMatch || isLoading) return;
    setIsLoading(true);
    try {
      await onConfirm(subtreeOption ? deleteSubtree : undefined);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <Modal open={true} onClose={onCancel} title={title} closeLabel={t('close')}
      footer={<>
        <Button variant="ghost" onClick={onCancel} disabled={isLoading}>{t('cancel')}</Button>
        <Button
          variant="danger"
          onClick={handleConfirm}
          disabled={!isMatch || isLoading}
        >
          {isLoading ? t('deleting') : t('deleteType', { itemType: t(itemType) })}
        </Button>
      </>}
    >
      <div className="text-ui-base text-text-muted leading-[1.6] mb-3">
        <InterpolatedName template={warningText} itemName={itemName} />
      </div>
      {subtreeOption && (
        <div className="text-ui-base mb-3">
          <FieldCheckbox
            checked={deleteSubtree}
            onChange={setDeleteSubtree}
            label={t('deleteSubtree', {
              docs: subtreeOption.childDocs,
              refs: subtreeOption.childRefs,
            })}
          />
        </div>
      )}
      <div>
        <div className="field-label">
          <InterpolatedName template={confirmLabel} itemName={itemName} />
        </div>
        <FieldInput
          autoFocus
          type="text"
          value={confirmText}
          onChange={(e) => setConfirmText(e.target.value)}
        />
      </div>
    </Modal>
  );
}
