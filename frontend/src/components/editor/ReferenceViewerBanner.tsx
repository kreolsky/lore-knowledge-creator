/** Header banner when viewing a reference — shows parent doc name + context-dependent actions.
 * INVARIANT: receives `reference` as a prop (deferred from Editor.tsx activeReference) so the
 * banner disappears in the same deferred commit as the media bar and editor remount. Reading
 * currentReference from the store directly would split the swap across two frames.  Why: the banner takes `reference` as a deferred prop (same commit as the media bar/editor remount); reading the store directly would split the swap across two frames (flicker). */

import { Plus, Trash2, ArrowLeft, ExternalLink } from 'lucide-react';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { emit } from '../../events';
import { useEditorContent } from '../../editor/active-editor';
import { Button, DownloadMenu } from '../ui';
import { useTranslation } from '../../i18n';
import { useReferenceDelete } from '../../hooks/useReferenceDelete';
import { referenceFileUrl } from '../../utils/reference-url';
import { isConvertedPdf, referenceDownloadProps } from '../references/ref-utils';
import type { Reference } from '../../types';

interface Props {
  reference: Reference;
  isReadonly: boolean;
  isEmpty: boolean;
  backLabel?: string;
  onBack: () => void;
  // Public-share surface (/s/:token): hide DownloadMenu + all edit actions, keep
  // only the back button (and force it visible — an empty ref would otherwise
  // render no back button on readonly). Authed callers omit this (default false).
  isPublicShare?: boolean;
}

export function ReferenceViewerBanner({ reference, isReadonly, isEmpty, backLabel, onBack, isPublicShare = false }: Props) {
  const { t } = useTranslation();
  const getEditorContent = useEditorContent();
  const currentDocument = useAppStore(s => s.currentDocument);
  const currentProject = useAppStore(s => s.currentProject);

  const setCurrentReference = useAppStore(s => s.setCurrentReference);
  const removeReference = useAppStore(s => s.removeReference);
  const { scheduleDelete } = useReferenceDelete();

  const isMarkdown = reference.media_type === 'markdown';
  const isFile = reference.media_type === 'file';
  // 'file' counts as media: a file ref is content-less (isEmpty), so without this
  // the delete + back buttons would vanish on an empty file reference.
  const isMedia = reference.media_type === 'audio' || reference.media_type === 'image' || isFile;
  // Converted PDF gets an Open-original action here (the server serves PDFs
  // Content-Disposition: inline, so a new tab renders it).
  const isPdf = isConvertedPdf(reference);
  const download = referenceDownloadProps(reference);

  const handleDelete = () => {
    const refId = reference.reference_id;
    const ref = useAppStore.getState().references.find(r => r.reference_id === refId);
    if (!ref) return;
    removeReference(refId);
    setCurrentReference(null);
    scheduleDelete(ref);
  };

  const handleCreateDocument = async () => {
    if (!currentProject) return;
    try {
      const refTitle = reference.title.replace(/\.[^.]+$/, '') || reference.title;
      const newDoc = await apiClient.post('/documents', {
        project_id: currentProject.project_id,
        parent_id: currentDocument!.document_id,
        title: refTitle,
        content: getEditorContent(),
      });
      setCurrentReference(null);
      emit('navigate-to-document', { documentId: newDoc.document_id });
    } catch (err) {
      console.error('Failed to create document from reference', err);
      useAppStore.getState().showToast(t('failedToCreateDocumentFromReference'), 'error');
    }
  };

  const showEmptyMarkdown = isMarkdown && isEmpty && !isReadonly;
  const showEmptyMedia = isMedia && isEmpty && !isReadonly;
  const hasText = !isEmpty;
  // The header delete acts on the WHOLE reference and only for a ref with
  // neither text nor file: a ref that still has a file drops it on the media
  // (MediaFileActions) or via the Refs-list trash — never here.
  const showDelete = isEmpty && !reference.file_path && !isReadonly;

  return (
    <div className="snapshot-preview-banner reference-viewer-banner">
      <div className="flex gap-1.5 items-center ml-auto">
        {showDelete && (
          <Button
            variant="ghost"
            size="sm"
            danger
            onClick={handleDelete}
          >
            <Trash2 size={13} /> {t('delete')}
          </Button>
        )}
        {isPdf && reference.file_path && (
          <Button variant="ghost" size="sm" onClick={() => window.open(referenceFileUrl(reference.reference_id, reference.file_path!), '_blank', 'noopener,noreferrer')}>
            <ExternalLink size={13} /> {t('openOriginal')}
          </Button>
        )}
        {!isPublicShare && (
          <DownloadMenu
            documentId={reference.reference_id}
            hasText={hasText}
            theme="blue"
            original={download.original}
            exportFormats={download.exportFormats}
          />
        )}
        {hasText && !isReadonly && (
          <Button variant="ghost" size="sm" onClick={handleCreateDocument}>
            <Plus size={13} /> {t('createDocument')}
          </Button>
        )}
        {(hasText || showEmptyMarkdown || showEmptyMedia || isPublicShare) && (
          <Button variant="ghost" size="sm" onClick={onBack}>
            <ArrowLeft size={13} /> {backLabel ?? currentDocument!.title}
          </Button>
        )}
      </div>
    </div>
  );
}
