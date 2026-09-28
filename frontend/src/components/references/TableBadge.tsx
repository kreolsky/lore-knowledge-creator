/**
 * Table badge — a document's table rendered as a References-panel list row, styled like a
 * reference pill. Plan: tables-as-references-badges.
 *
 * see SYSTEM: table-block — the panel-facing list row for a table (analogous to RefCard for a
 * reference). A table is NOT a separate entity; its identity + shape come from the live
 * ydoc via `useDocumentTables`. Click opens the table in the center focus view (via
 * `setCurrentTable`); the armed delete removes the whole table (anchor + model) via
 * `deleteTableFromDoc`; the Pencil starts an inline label rename (via `renameTable`).
 *
 * ARCH: reuses the shared ListRow/ListPill/RowActions shell + useArmedAction so the badge
 * is typographically and behaviorally identical to a RefCard (one owner per concern). The
 * rename UI mirrors RefCard exactly: Pencil in RowActions + FieldInput body override +
 * double-click start. No rounded corners (project convention).
 */

import { memo, useRef } from 'react';
import { Table, Trash2, Pencil, FileInput, Download } from 'lucide-react';
import { useTranslation } from '../../i18n';
import type { DocumentTableEntry } from '../editor/live-preview/table-block-model';
import { IconButton, FieldInput, ListPill, RowActions } from '../ui';
import { ListRow } from '../chat/ListRow';
import { useArmedAction } from '../../hooks/useArmedAction';

interface TableBadgeProps {
  entry: DocumentTableEntry;
  isActive: boolean;
  canEdit: boolean;
  renamingId: string | null;
  renameValue: string;
  onSelect: (entry: DocumentTableEntry) => void;
  onDelete: (entry: DocumentTableEntry) => void;
  onInsert: (entry: DocumentTableEntry) => void;
  onDownload: (entry: DocumentTableEntry) => void;
  onStartRename: (entry: DocumentTableEntry) => void;
  onRenameChange: (v: string) => void;
  onRenameCommit: (entry: DocumentTableEntry) => void;
  onRenameCancel: () => void;
  onHover?: (entry: DocumentTableEntry, el: HTMLElement) => void;
  onHoverLeave?: (e?: React.MouseEvent) => void;
}

export const TableBadge = memo(function TableBadge({
  entry, isActive, canEdit,
  renamingId, renameValue,
  onSelect, onDelete, onInsert, onDownload,
  onStartRename, onRenameChange, onRenameCommit, onRenameCancel,
  onHover, onHoverLeave,
}: TableBadgeProps) {
  const { t } = useTranslation();
  const deleteAction = useArmedAction();
  const cardRef = useRef<HTMLDivElement>(null);

  // Download CSV is read-only export — available to viewers too (not gated by canEdit).
  const downloadBtn = (
    <IconButton
      size="sm"
      title={t('tableBadgeDownload')}
      onClick={e => { e.stopPropagation(); onDownload(entry); }}
    >
      <Download size={13} />
    </IconButton>
  );

  const editMenu = canEdit ? (
    <>
      <IconButton
        size="sm"
        danger
        filled={deleteAction.armed}
        title={deleteAction.armed ? t('tableBadgeDeleteConfirm') : t('delete')}
        onClick={e => {
          e.stopPropagation();
          deleteAction.handleClick(() => onDelete(entry));
        }}
        onMouseLeave={deleteAction.disarm}
      >
        <Trash2 size={13} />
      </IconButton>
      {/* Unlinked = no anchor in the text: offer to re-insert the transclusion so the
          table is reachable in the document again. */}
      {entry.unlinked && (
        <IconButton
          size="sm"
          title={t('tableBadgeInsert')}
          onClick={e => { e.stopPropagation(); onInsert(entry); }}
        >
          <FileInput size={13} />
        </IconButton>
      )}
      <IconButton
        size="sm"
        title={t('tableBadgeRename')}
        onClick={e => { e.stopPropagation(); onStartRename(entry); }}
      >
        <Pencil size={13} />
      </IconButton>
    </>
  ) : undefined;

  // Merge edit actions + the always-on download action into one RowActions slot.
  const menu = editMenu ? (
    <>
      {editMenu}
      {downloadBtn}
    </>
  ) : downloadBtn;

  return (
    <ListPill
      variant="plain"
      active={isActive}
      activeColor="blue"
      clickable
      onClick={() => onSelect(entry)}
      onMouseEnter={() => { if (cardRef.current && onHover) onHover(entry, cardRef.current); }}
      onMouseLeave={(e) => { if (onHoverLeave) onHoverLeave(e); }}
    >
      <div ref={cardRef}>
        <ListRow
          body={entry.label || t('tableBadgeUntitled')}
          bodyOverride={renamingId === entry.table_id ? (
            <FieldInput
              className="ref-rename-input"
              data-rename-input
              value={renameValue}
              onChange={e => onRenameChange(e.target.value)}
              autoFocus
              onKeyDown={e => {
                if (e.key === 'Enter') { e.preventDefault(); onRenameCommit(entry); }
                if (e.key === 'Escape') { onRenameCancel(); }
              }}
              onBlur={() => onRenameCommit(entry)}
              onClick={e => e.stopPropagation()}
            />
          ) : undefined}
          meta={entry.unlinked ? `${entry.rows} × ${entry.cols} · ${t('tableBadgeUnlinked')}` : `${entry.rows} × ${entry.cols}`}
          metaEmphasized={isActive}
          metaIcon={<Table size={13} className="text-text-dim" />}
          actions={<RowActions active={isActive} activeBackdrop="blue" menu={menu} />}
          onBodyDoubleClick={canEdit ? (e) => { e.stopPropagation(); onStartRename(entry); } : undefined}
        />
      </div>
    </ListPill>
  );
});
