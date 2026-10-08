/** Media bar for references with uploaded files: image preview (with a hover
 * overlay of file actions), audio player, file download card — each carrying
 * MediaFileActions (download + armed file delete) so the file's actions live ON
 * the media, not in the header. On the public-share surface (/s/:token) the
 * same bar renders with canEdit=false (download only) and referenceFileUrl
 * routes through the anonymous /api/public/{token}/files/… surface. A PDF ref
 * (markdown + .pdf file_path) renders nothing here — its open-original action
 * lives in the banner, beside the download menu. Audio uses the custom
 * AudioPlayer (not native <audio controls>) so the speed menu can open
 * downward — see AudioPlayer.tsx for why.
 */

import { FileArchive } from 'lucide-react';
import { useTranslation } from '../../i18n';
import { formatFileSize } from '../references/ref-utils';
import { Reference } from '../../types';
import { referenceFileUrl } from '../../utils/reference-url';
import { AudioPlayer } from './AudioPlayer';
import { MediaFileActions } from './MediaFileActions';

interface Props {
  reference: Reference;
  /** Delete is an edit action — viewers and public shares get download only. */
  canEdit: boolean;
}

export function ReferenceMediaBar({ reference, canEdit }: Props) {
  const { t } = useTranslation();
  if (!reference.file_path) return null;

  // referenceFileUrl routes through the anonymous /api/public/{token}/files/…
  // when a public-share context is active (avoids the authed 401 on /s/:token).
  const fileUrl = referenceFileUrl(reference.reference_id, reference.file_path);

  if (reference.media_type === 'image') {
    return (
      <div className="flex justify-center px-4">
        {/* relative group — the actions plate sits over the picture's top-right
            corner and shows on hover / focus-within, and always on devices without
            hover (touch) so a tap never arms an invisible delete; bg-surface plate so
            the icons read over any picture. No rounded corners (project rule).
            WHY: the width cap sits on the wrapper, not the <img> — a percentage
            cap on the img resolves against a wrapper sized BY the img, so a
            large file stretched the wrapper to the full row: the picture went
            left and the plate floated past its right edge.
            WHY: the plate rides a zero-height sticky row (not absolute) so it stays
            pinned to the viewport's top while a tall picture scrolls under it, and
            stops at the picture's bottom; overflow-clip (not hidden — hidden would
            make the wrapper the sticky scroller) keeps it from spilling past. */}
        <div className="relative group overflow-clip max-w-[clamp(720px,100%,864px)]">
          <div className="sticky top-0 z-10 h-0 flex items-start justify-end">
            <div className="mt-2 mr-2 flex items-center gap-1 p-1 bg-surface opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100">
              <MediaFileActions reference={reference} canEdit={canEdit} size="md" />
            </div>
          </div>
          <img
            src={fileUrl}
            alt={reference.title}
            className="block object-contain max-w-full"
          />
        </div>
      </div>
    );
  }

  if (reference.media_type === 'file') {
    const originalName = reference.file_meta?.original_name || reference.title;
    return (
      <div className="flex justify-center py-2 border-b border-border-soft bg-surface2">
        <div className="w-full px-[10px] flex justify-center max-w-[clamp(720px,100%,864px)]">
          <div className="flex items-center gap-3 px-3 py-2 border border-border-soft bg-surface min-w-0 max-w-full">
            <FileArchive size={20} className="shrink-0 text-text-dim" />
            <div className="min-w-0">
              <div className="text-ui-base truncate">{originalName}</div>
              <div className="text-ui-xs text-text-dim">
                {reference.file_meta?.file_size !== undefined
                  ? formatFileSize(reference.file_meta.file_size)
                  : t('fileReference')}
              </div>
            </div>
            <div className="ml-2 flex items-center gap-1 shrink-0">
              <MediaFileActions reference={reference} canEdit={canEdit} />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (reference.media_type !== 'audio') return null;

  return (
    <div className="flex justify-center py-2 border-b border-border-soft bg-surface2">
      <div className="w-full px-[10px] max-w-[clamp(720px,100%,864px)]">
        <AudioPlayer
          src={fileUrl}
          title={reference.title}
          durationSec={reference.file_meta?.duration_sec}
          downloadUrl={fileUrl}
          downloadName={reference.file_meta?.original_name || reference.title}
          trailing={<MediaFileActions reference={reference} canEdit={canEdit} showDownload={false} />}
        />
      </div>
    </div>
  );
}
