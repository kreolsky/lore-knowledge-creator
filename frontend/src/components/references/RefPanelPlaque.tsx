/**
 * Plaque over a reference opened INSIDE the Refs tab ('panel' open mode) — the
 * reference twin of the open-chat header (ChatHeader.tsx):
 * `[← Refs] [parent slot] title … [eye (pressed)] [download ▾] [restore?] [trash]`.
 * Owns no state but the armed trash; every action is a callback of ReferencesPanel —
 * including the title rename (double-click, same shape as the chat header), whose
 * state is the panel's `renamingId`/`renameValue` shared with RefCard.
 * Store slices: documents (parent title only).
 */
import { ArrowLeft, FileText, FolderUp, ArchiveRestore, Trash2, Eye } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { useArmedAction } from '../../hooks/useArmedAction';
import { Button, IconButton, DownloadMenu, FieldInput } from '../ui';
import { useTranslation } from '../../i18n';
import { referenceDownloadProps, ACTIVE_TOGGLE_CLS } from './ref-utils';
import { hasReferenceContent, type Reference } from '../../types';

interface Props {
  reference: Reference;
  canEdit: boolean;
  currentDocumentId: string | null;
  onBack: () => void;
  /** Leave 'panel' mode: the mode goes back to center and the open reference moves there. */
  onExitPanel: () => void;
  onGotoParent: (documentId: string) => void;
  onChangeParent: (reference: Reference, anchorRect: DOMRect) => void;
  onArchive: (reference: Reference) => void;
  onRestore: (reference: Reference) => void;
  onDelete: (referenceId: string) => void;
  renamingId: string | null;
  renameValue: string;
  onStartRename: (reference: Reference) => void;
  onRenameChange: (value: string) => void;
  onRenameCommit: (reference: Reference) => void;
  onRenameCancel: () => void;
}

export function RefPanelPlaque({
  reference, canEdit, currentDocumentId, onBack, onExitPanel, onGotoParent, onChangeParent, onArchive, onRestore, onDelete,
  renamingId, renameValue, onStartRename, onRenameChange, onRenameCommit, onRenameCancel,
}: Props) {
  const { t } = useTranslation();
  const documents = useAppStore(s => s.documents);
  const deleteAction = useArmedAction();

  const parentId = reference.document_id || null;
  const showGotoParent = !!parentId && parentId !== currentDocumentId;
  const showChangeParent = canEdit && !!parentId && parentId === currentDocumentId;
  const parentTitle = parentId ? documents.find(d => d.document_id === parentId)?.title ?? '' : '';
  const archived = reference.archived === true;
  const download = referenceDownloadProps(reference);

  return (
    <div data-testid="refs-panel-plaque" className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
      <Button variant="yellow" size="sm" onClick={onBack}>
        <ArrowLeft size={13} />
        {t('references')}
      </Button>

      {/* WHY this 22×22 slot (IconButton size="sm" footprint) is ALWAYS rendered
          between the "← Refs" button and the title with AT MOST one icon —
          goto-parent / change-parent / empty: the title's left edge must not
          shift across states. The two icon branches are mutually exclusive by
          construction (goto-parent requires parent ≠ open document, change-parent
          requires parent == open document), so the slot never holds two icons. */}
      <div data-title-slot className="flex items-center justify-center w-[22px] h-[22px] shrink-0">
        {showGotoParent && (
          <IconButton size="sm" title={t('gotoParentDoc', { title: parentTitle })} onClick={() => onGotoParent(parentId!)}>
            <FileText size={14} />
          </IconButton>
        )}
        {showChangeParent && (
          <IconButton size="sm" title={t('changeParentDocument')} onClick={e => onChangeParent(reference, e.currentTarget.getBoundingClientRect())}>
            <FolderUp size={14} />
          </IconButton>
        )}
      </div>

      {renamingId === reference.reference_id ? (
        // WHY the CHAT pair, not .ref-rename-input: the plaque title is the chat
        // header's box (text-sm / weight 500 / 20px line, no side padding), and
        // .chat-rename-input is built to be pixel-identical to that span, so
        // entering edit mode moves nothing. .ref-rename-input is the LIST card's
        // weight-400 / 80% box and would thin and shift the title here.
        <FieldInput
          className="doc-rename-input chat-rename-input flex-1 min-w-0"
          data-rename-input
          value={renameValue}
          onChange={e => onRenameChange(e.target.value)}
          autoFocus
          onKeyDown={e => {
            if (e.key === 'Enter') { e.preventDefault(); onRenameCommit(reference); }
            if (e.key === 'Escape') { onRenameCancel(); }
          }}
          onBlur={() => onRenameCommit(reference)}
        />
      ) : (
        // Rename entry point is DOUBLE-CLICK only (chat-header contract); the native
        // tooltip carries the hint. Read-only viewers get the plain title tooltip.
        <span
          data-ref-title
          className={`flex-1 text-sm font-medium truncate min-w-0 ${archived ? 'opacity-50' : ''}`}
          title={canEdit ? `${reference.title} — ${t('chatRenameHint')}` : reference.title}
          onDoubleClick={canEdit ? () => onStartRename(reference) : undefined}
        >
          {reference.title}
        </span>
      )}

      {/* The pressed eye = the panel toggle in its active state; clicking it turns panel
          mode off and the reference re-opens in the center. */}
      <IconButton size="sm" className={ACTIVE_TOGGLE_CLS} title={t('toggleRefInPanel')} onClick={onExitPanel}>
        <Eye size={14} />
      </IconButton>
      <DownloadMenu
        documentId={reference.reference_id}
        hasText={hasReferenceContent(reference)}
        original={download.original}
        exportFormats={download.exportFormats}
        iconOnly
      />
      {/* Restore sits directly LEFT of the trash (the pair reads as one archive control);
          the download menu moves left to make room. */}
      {canEdit && archived && (
        <IconButton size="sm" title={t('restoreReference')} onClick={() => onRestore(reference)}>
          <ArchiveRestore size={14} />
        </IconButton>
      )}
      {canEdit && (
        // Stateful trash, same contract as RefCard: live → single-click archive (no
        // arming, non-destructive); archived → armed two-click permanent delete.
        <IconButton
          size="sm"
          danger
          filled={deleteAction.armed}
          title={archived ? t('deleteReferencePermanently') : t('archiveReference')}
          onClick={() => {
            if (archived) deleteAction.handleClick(() => onDelete(reference.reference_id));
            else onArchive(reference);
          }}
          onMouseLeave={deleteAction.disarm}
        >
          <Trash2 size={14} />
        </IconButton>
      )}
    </div>
  );
}
