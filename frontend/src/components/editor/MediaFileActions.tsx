/** File actions rendered ON the media surface (image hover overlay, audio row,
 * archive card): download the original + armed delete of the file. The header
 * (ReferenceViewerBanner / RefPanelPlaque) acts on the TEXT only — this
 * component is the single home of the file's own actions.
 *
 * WHY: reads the reference from props, never the store — it renders inside
 * the deferred layout commit (same frame as the media bar / editor remount), and
 * a store read would split the swap across two frames.
 */

import { Download, Trash2 } from 'lucide-react';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useReferenceFileDelete } from '../../hooks/useReferenceFileDelete';
import { useTranslation } from '../../i18n';
import { IconButton } from '../ui';
import { referenceFileUrl } from '../../utils/reference-url';
import type { Reference } from '../../types';

interface Props {
  reference: Reference;
  canEdit: boolean;
  /** The audio row keeps the player's OWN download link — render the delete alone there. */
  showDownload?: boolean;
}

export function MediaFileActions({ reference, canEdit, showDownload = true }: Props) {
  const { t } = useTranslation();
  const { deleteFile } = useReferenceFileDelete();
  const deleteAction = useArmedAction();

  const originalName = reference.file_meta?.original_name || reference.title;
  const fileUrl = referenceFileUrl(reference.reference_id, reference.file_path!);
  // Delete is hidden while a transcription/conversion job is running on the file
  // (uploading/processing) — dropping the bytes under a live worker leaves it
  // acting on a deleted file.
  const jobRunning = reference.processing_status === 'uploading' || reference.processing_status === 'processing';

  return (
    <>
      {showDownload && (
        <a
          href={fileUrl}
          download={originalName}
          title={t('download')}
          aria-label={t('download')}
          className="flex items-center justify-center w-[22px] h-[22px] text-text-muted hover:text-text hover:bg-surface3"
        >
          <Download size={14} />
        </a>
      )}
      {canEdit && !jobRunning && (
        // Stateful trash, same armed pattern as RefPanelPlaque: first click arms
        // (filled), second click executes, mouse-leave disarms.
        <IconButton
          size="sm"
          danger
          filled={deleteAction.armed}
          title={t('deleteFile')}
          aria-label={t('deleteFile')}
          onClick={() => deleteAction.handleClick(() => deleteFile(reference))}
          onMouseLeave={deleteAction.disarm}
        >
          <Trash2 size={14} />
        </IconButton>
      )}
    </>
  );
}
