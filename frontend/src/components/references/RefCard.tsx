/**
 * Single reference pill — title, "type | parent" meta, status badge, and action buttons.
 *
 * ARCH: title + meta rows are rendered via the shared ListRow (same component as
 * ChatRow / NoteSessionCard), NOT hand-rolled here. A previous bespoke .title/.titleRow
 * re-declared font-size/line-height and drifted from chat/notes twice (line-height gap,
 * dead border). RefCard passes its extras through ListRow slots so typography has ONE
 * owner: actions (RowActions overlay) → ListRow `actions`, StatusBadge + file size →
 * `metaExtra`, inline rename input → `bodyOverride`, open-reference meta emphasis →
 * `metaEmphasized`, double-click rename → `onBodyDoubleClick`.
 */

import { memo, useRef } from 'react';
import type React from 'react';
import { FileInput, Pencil, Trash2, ArchiveRestore, RotateCcw, FolderUp, Bot, MessagesSquare } from 'lucide-react';
import { useTranslation } from '../../i18n';
import { Reference, hasReferenceContent } from '../../types';
import { StatusBadge, formatFileSize, hueForId, RefIcon } from './ref-utils';
import { formatDate } from '../../utils/format';
import { IconButton, FieldInput, ListPill, RowActions } from '../ui';
import { ListRow } from '../chat/ListRow';
import { useEditorView } from '../../editor/active-editor';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useAppStore } from '../../store/app-store';
import styles from './RefCard.module.css';

interface RefCardProps {
  reference: Reference;
  fading?: boolean;
  isActive: boolean;
  canEdit: boolean;
  renamingId: string | null;
  renameValue: string;
  scopeLabel: string | null;
  onSelect: (ref: Reference) => void;
  onDelete: (refId: string) => void;
  // Plan reference-archive-v2: staged-delete stage-1 handlers. `onArchive` (live ref →
  // archived, single click, no arming) and `onRestore` (archived → live). The trash icon
  // is STATEFUL: live → onArchive; archived → armed onDelete (stage-2 soft-delete).
  onArchive: (ref: Reference) => void;
  onRestore: (ref: Reference) => void;
  onStartRename: (ref: Reference) => void;
  onRenameChange: (v: string) => void;
  onRenameCommit: (ref: Reference) => void;
  onRenameCancel: () => void;
  onRetry: (e: React.MouseEvent, ref: Reference) => void;
  onRetryConfirm: (ref: Reference) => void;
  onChangeParent: (ref: Reference, anchorRect: DOMRect) => void;
  onSendToAgent?: (ref: Reference) => void;
  onChatWithReference?: (ref: Reference) => void;
  agentRunning?: boolean;
  onRefHover?: (refId: string, el: HTMLElement) => void;
  onRefHoverLeave?: (e?: React.MouseEvent) => void;
}

// WHY memo: reference list re-renders on any ref mutation; card props are stable by ref identity
export const RefCard = memo(function RefCard({
  reference, fading, isActive, canEdit, renamingId, renameValue, scopeLabel,
  onSelect, onDelete, onArchive, onRestore,
  onStartRename, onRenameChange, onRenameCommit, onRenameCancel,
  onRetry, onRetryConfirm, onChangeParent,
  onSendToAgent, onChatWithReference, agentRunning,
  onRefHover, onRefHoverLeave,
}: RefCardProps) {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const deleteAction = useArmedAction();
  const cardRef = useRef<HTMLDivElement>(null);
  const isUploading = reference.processing_status === 'uploading';
  // "Is it me" guard for the author nick: compare by id, never by name (names are
  // user-editable and not unique). Own references show no author segment.
  const currentUserId = useAppStore(s => s.currentUser?.user_id);
  // Gray meta, unified with the chat row's "date | …" order: last-modified datetime,
  // then the foreign-parent label when out of scope. Type is now the leading metaIcon.
  const baseMeta = formatDate(reference.updated_at);
  const isImage = reference.media_type === 'image';
  // Restore the colored scope-label plate (parity with ImageGallery.tsx): the parent
  // document name gets an hsl(hueForId(document_id)) background, inline within the meta
  // row so spacing is preserved. Only for foreign references (scopeLabel !== null).
  const parentHue = reference.document_id ? hueForId(reference.document_id) : 0;
  // Author nick: the reference CREATOR's nickname, shown ONLY for someone else's
  // reference. Plain text in the row's dim color — no colored plate (that treatment is
  // exclusive to the parent-document label). Absent for own refs and for
  // impersonal/legacy creations (no "System" fallback).
  const authorNick =
    reference.created_by && reference.created_by_name && reference.created_by !== currentUserId
      ? reference.created_by_name
      : null;
  // Meta row: up to three segments (date, parent plate, author nick) joined by ` | `, so
  // both shapes — foreign ref (`date | plate | nick`) and open-doc ref (`date | nick`) —
  // fall out of ONE join instead of two hand-written branches.
  const metaSegments: React.ReactNode[] = [];
  if (baseMeta) metaSegments.push(baseMeta);
  if (scopeLabel) {
    metaSegments.push(
      <span
        key="plate"
        className="inline-block leading-none px-1 py-0.5 -my-0.5"
        style={{ background: `hsl(${parentHue}, 60%, 92%)`, color: `hsl(${parentHue}, 50%, 35%)` }}
      >
        {scopeLabel}
      </span>,
    );
  }
  if (authorNick) metaSegments.push(authorNick);
  const metaChildren: React.ReactNode[] = [];
  metaSegments.forEach((seg, i) => {
    if (i > 0) metaChildren.push(' | ');
    metaChildren.push(seg);
  });
  const meta: React.ReactNode = metaChildren.length ? metaChildren : null;

  const primary = (
    <>
      {canEdit && onSendToAgent && hasReferenceContent(reference) && (
        <IconButton
          size="sm"
          title={agentRunning ? t('agentExtracting') : t('sendToAgent')}
          disabled={agentRunning}
          onClick={e => { e.stopPropagation(); onSendToAgent(reference); }}
        >
          <Bot size={13} />
        </IconButton>
      )}
      {onChatWithReference && (
        <IconButton
          size="sm"
          title={t('chatWithReference')}
          onClick={e => { e.stopPropagation(); onChatWithReference(reference); }}
        >
          <MessagesSquare size={13} />
        </IconButton>
      )}
    </>
  );

  const menu = canEdit ? (
    <>
      {/* see SYSTEM: transclusion — insert embed for ALL references. Image refs embed via
          ImageWidget; non-image refs embed their content as markdown via TransclusionWidget. */}
      <IconButton
        size="sm"
        title={t('insertEmbedAtCursor')}
        onClick={e => {
          e.stopPropagation();
          const view = getEditorView();
          if (!view) return;
          const pos = view.state.selection.main.head;
          const text = `![${reference.title}](ref:${reference.reference_id})`;
          view.dispatch({
            changes: { from: pos, insert: text },
            selection: { anchor: pos + 2 + reference.title.length },
          });
          view.focus();
        }}
      >
        <FileInput size={13} />
      </IconButton>
      {reference.media_type === 'audio' && (reference.processing_status === 'error' || reference.processing_status === 'ready' || reference.processing_status === 'processing') && (
        <IconButton
          size="sm"
          title={t('retryTranscription')}
          onClick={e => {
            e.stopPropagation();
            if (reference.processing_status === 'ready') { onRetryConfirm(reference); }
            else { onRetry(e, reference); }
          }}
        >
          <RotateCcw size={13} />
        </IconButton>
      )}
      {/* Plan reference-archive-v2: stateful trash. Archived ref → Restore icon (single
          click, no arming) sits LEFT of the trash; the trash becomes the armed stage-2
          soft-delete (title reflects the destructive intent). Live ref → trash is a
          single-click archive (no arming, non-destructive). */}
      {reference.archived === true && (
        <IconButton
          size="sm"
          title={t('restoreReference')}
          onClick={e => { e.stopPropagation(); onRestore(reference); }}
        >
          <ArchiveRestore size={13} />
        </IconButton>
      )}
      <IconButton
        size="sm"
        danger
        filled={deleteAction.armed}
        title={reference.archived === true ? t('deleteReferencePermanently') : t('archiveReference')}
        onClick={e => {
          e.stopPropagation();
          if (reference.archived === true) {
            // Stage-2: armed soft-delete (destructive, two-click).
            deleteAction.handleClick(() => onDelete(reference.reference_id));
          } else {
            // Stage-1: single-click archive (non-destructive, no arming).
            onArchive(reference);
          }
        }}
        onMouseLeave={deleteAction.disarm}
      >
        <Trash2 size={13} />
      </IconButton>
      <IconButton
        size="sm"
        title={t('rename')}
        onClick={e => { e.stopPropagation(); onStartRename(reference); }}
      >
        <Pencil size={13} />
      </IconButton>
      <IconButton
        size="sm"
        title={t('changeParentDocument')}
        onClick={e => { e.stopPropagation(); onChangeParent(reference, e.currentTarget.getBoundingClientRect()); }}
      >
        <FolderUp size={13} />
      </IconButton>
    </>
  ) : undefined;

  return (
    <ListPill
      variant="plain"
      active={isActive}
      activeColor="blue"
      clickable={!isUploading}
      className={`${fading ? styles.fading : ''} ${isUploading ? 'opacity-70' : ''} ${reference.archived ? 'opacity-50' : ''}`}
      // Drag-reorder attributes (useSiblingDragReorder + refDragAdapter): the
      // row id, and the group = the HOST document. Archived and unhosted cards
      // carry no group attribute → neither draggable nor a drop target.
      data-ref-id={reference.reference_id}
      data-ref-group={
        reference.archived === true || !reference.document_id ? undefined : reference.document_id
      }
      onClick={() => { if (!isUploading) onSelect(reference); }}
      onMouseEnter={() => {
        if (!isUploading && cardRef.current && onRefHover) onRefHover(reference.reference_id, cardRef.current);
      }}
      onMouseLeave={(e) => { if (onRefHoverLeave) onRefHoverLeave(e); }}
    >
      <div ref={cardRef}>
        <ListRow
          body={reference.title}
          bodyOverride={renamingId === reference.reference_id ? (
            <FieldInput
              className="ref-rename-input"
              data-rename-input
              value={renameValue}
              onChange={e => onRenameChange(e.target.value)}
              autoFocus
              onKeyDown={e => {
                if (e.key === 'Enter') { e.preventDefault(); onRenameCommit(reference); }
                if (e.key === 'Escape') { onRenameCancel(); }
              }}
              onBlur={() => onRenameCommit(reference)}
              onClick={e => e.stopPropagation()}
            />
          ) : undefined}
          meta={meta}
          metaEmphasized={isActive}
          metaIcon={
            <span className="flex items-center gap-1">
              <RefIcon item={reference} />
              {isImage && reference.file_meta?.file_size && (
                <span className="text-ui-2xs">{formatFileSize(reference.file_meta.file_size)}</span>
              )}
            </span>
          }
          metaExtra={
            <>
              <StatusBadge item={reference} />
              {reference.archived === true && (
                <span className="text-ui-2xs px-1 py-0.5 bg-surface3 text-text-dim border border-border">
                  {t('archivedBadge')}
                </span>
              )}
              {!isImage && reference.file_meta?.file_size && !reference.processing_status && (
                <span className="text-ui-2xs text-text-dim">
                  {formatFileSize(reference.file_meta.file_size)}
                </span>
              )}
            </>
          }
          actions={<RowActions active={isActive} activeBackdrop="blue" primary={primary} menu={menu} />}
          onBodyDoubleClick={canEdit ? (e) => { e.stopPropagation(); onStartRename(reference); } : undefined}
        />
      </div>
    </ListPill>
  );
});
