/** Grid gallery for image-type references — square thumbnails with hover action overlay. */

import { memo, useRef } from 'react';
import type React from 'react';
import { FileInput, Trash2, ArchiveRestore, FolderUp } from 'lucide-react';
import { Reference } from '../../types';
import { IconButton } from '../ui';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useTranslation } from '../../i18n';
import { useEditorView } from '../../editor/active-editor';
import { referenceThumbUrl } from '../../utils/reference-url';
import { hueForId } from './ref-utils';
import styles from './ImageGallery.module.css';

interface ImageGalleryProps {
  references: Reference[];
  activeRefId: string | null;
  canEdit: boolean;
  getLabel: (ref: Reference) => string | null;
  onSelect: (ref: Reference) => void;
  onDelete: (refId: string) => void;
  // Plan reference-archive-v2: staged-delete handlers (mirror RefCard's stateful trash).
  onArchive: (ref: Reference) => void;
  onRestore: (ref: Reference) => void;
  onChangeParent: (ref: Reference, anchorRect: DOMRect) => void;
  onRefHover?: (refId: string, el: HTMLElement) => void;
  onRefHoverLeave?: (e?: React.MouseEvent) => void;
}

export const ImageGallery = memo(function ImageGallery({
  references, activeRefId, canEdit, getLabel,
  onSelect, onDelete, onArchive, onRestore, onChangeParent,
  onRefHover, onRefHoverLeave,
}: ImageGalleryProps) {
  if (references.length === 0) return null;

  return (
    <div className={styles.grid}>
      {references.map((ref) => (
        <ImageThumb
          key={ref.reference_id}
          reference={ref}
          isActive={activeRefId === ref.reference_id}
          canEdit={canEdit}
          label={getLabel(ref)}
           onSelect={onSelect}
           onDelete={onDelete}
           onArchive={onArchive}
           onRestore={onRestore}
           onChangeParent={onChangeParent}
          onRefHover={onRefHover}
          onRefHoverLeave={onRefHoverLeave}
        />
      ))}
    </div>
  );
});

interface ImageThumbProps {
  reference: Reference;
  isActive: boolean;
  canEdit: boolean;
  label: string | null;
  onSelect: (ref: Reference) => void;
  onDelete: (refId: string) => void;
  onArchive: (ref: Reference) => void;
  onRestore: (ref: Reference) => void;
  onChangeParent: (ref: Reference, anchorRect: DOMRect) => void;
  onRefHover?: (refId: string, el: HTMLElement) => void;
  onRefHoverLeave?: (e?: React.MouseEvent) => void;
}

const ImageThumb = memo(function ImageThumb({
  reference, isActive, canEdit, label,
  onSelect, onDelete, onArchive, onRestore, onChangeParent,
  onRefHover, onRefHoverLeave,
}: ImageThumbProps) {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const deleteAction = useArmedAction();
  const thumbRef = useRef<HTMLDivElement>(null);
  const isUploading = reference.processing_status === 'uploading';
  const h = reference.document_id ? hueForId(reference.document_id) : 0;
  // WHY referenceThumbUrl (not a hardcoded /api/files/…): the public-share
  // surface reuses this same ImageGallery, and the authed thumb URL 401s
  // anonymous callers. `referenceThumbUrl` switches to the public surface
  // when `setPublicFileContext(token)` is active (PublicSharePage mount).
  const fileUrl = reference.file_path
    ? referenceThumbUrl(reference.reference_id)
    : '';

  const cls = [
    styles.thumb,
    isActive ? styles.thumbActive : '',
    isUploading ? styles.thumbUploading : '',
    // Plan reference-archive-v2: dim archived thumbs (parity with RefCard opacity-50).
    reference.archived ? 'opacity-50' : '',
  ].filter(Boolean).join(' ');

  return (
    <div
      ref={thumbRef}
      className={cls}
      onClick={() => { if (!isUploading) onSelect(reference); }}
      onMouseEnter={() => {
        if (!isUploading && thumbRef.current && onRefHover) onRefHover(reference.reference_id, thumbRef.current);
      }}
      onMouseLeave={(e) => { if (onRefHoverLeave) onRefHoverLeave(e); }}
      title={reference.title}
    >
      {fileUrl && <img src={fileUrl} alt={reference.title} className={styles.thumbImg} loading="lazy" decoding="async" />}
      {(isUploading || reference.processing_status === 'queued' || reference.processing_status === 'processing') && (
        <div className={styles.spinnerWrap}>
          <div className="w-4 h-4 border-2 border-white/30 border-t-white animate-spin" />
        </div>
      )}
      {label && (
        <div
          className={styles.label}
          style={{ background: `hsl(${h}, 60%, 92%)`, color: `hsl(${h}, 50%, 35%)` }}
        >
          {label}
        </div>
      )}
      {reference.archived === true && (
        // Plan reference-archive-v2: small "Archived" chip over an archived thumb.
        <div className="absolute top-1 left-1 text-ui-2xs px-1 py-0.5 bg-surface3 text-text-dim border border-border">
          {t('archivedBadge')}
        </div>
      )}
      {canEdit && (
        <div className={styles.overlay} onClick={e => e.stopPropagation()}>
          {/* WHY gate the ENTIRE overlay (the wrapper, not just its children) on
              canEdit: the .overlay div itself carries padding + background
              (.thumb:hover shows it). Rendering it childless but present still
              showed an empty colored strip at the bottom of every thumb on hover
              for viewer/commentator/public-share roles — looked like a broken
              action bar. Skipping the wrapper entirely removes the hover strip.
              Rules-of-hooks forbid conditional useEditorView/useArmedAction calls
              (they're already invoked unconditionally above); gating the overlay
              only makes their OUTPUTS unused on the readonly path, which is fine.
              The correct deeper refactor (splitting ImageThumb into readonly/
              editable variants) is out of scope here — see the plan §4. */}
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
            <FileInput size={11} />
          </IconButton>
          <IconButton
            size="sm"
            title={t('changeParentDocument')}
            onClick={e => {
              e.stopPropagation();
              onChangeParent(reference, e.currentTarget.getBoundingClientRect());
            }}
          >
            <FolderUp size={11} />
          </IconButton>
          {/* Plan reference-archive-v2: stateful trash (parity with RefCard). Archived →
              Restore icon (left of trash, single click) + armed soft-delete trash. Live →
              single-click archive trash. */}
          {reference.archived === true && (
            <IconButton
              size="sm"
              title={t('restoreReference')}
              onClick={e => { e.stopPropagation(); onRestore(reference); }}
            >
              <ArchiveRestore size={11} />
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
                deleteAction.handleClick(() => onDelete(reference.reference_id));
              } else {
                onArchive(reference);
              }
            }}
            onMouseLeave={deleteAction.disarm}
          >
            <Trash2 size={11} />
          </IconButton>
        </div>
      )}
    </div>
  );
});
