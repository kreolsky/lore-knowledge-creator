/** Shared pending-attachment strip: image thumbnails and file chips, one square
 * size. An image chip opens the lightbox at 1:1 — the version the model gets — and
 * removes by its corner button; a file chip removes on click. Used by both AI and notes composers (files:
 * AI only). `FileChip` alone also renders a sent message's files. */
// SYSTEM: chat-composer — shared composer attachment chips
// INVARIANT: no rounded corners anywhere (project rule) — the hover overlay uses
// a square backdrop, not rounded-full. Why: the AI composer historically used
// rounded-full here; unifying on the note style keeps the whole app square.

import { useState } from 'react';
import { AlertTriangle, File, FileArchive, FileAudio, FileSpreadsheet, FileText, Loader2, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { IconButton } from '../../ui';
import { ImageLightbox } from '../MessageBubble/internal/image-lightbox';
import { useTranslation } from '../../../i18n';
import type { PressedImageMeta } from '../../../chat/attach-image';
import type { PendingFile } from '../../../store/chat-store/types';

interface Props {
  images: string[];
  onRemove: (index: number) => void;
  imageMeta?: Record<string, PressedImageMeta>;
  /** Presses in flight: one busy chip each. */
  pressing?: number;
  files?: PendingFile[];
  onRemoveFile?: (key: string) => void;
}

const ARCHIVE_EXT = /\.(zip|7z|tar|gz|tgz|bz2|xz)$/i;
const SHEET_EXT = /\.(csv|tsv|xlsx?|ods)$/i;
const TEXT_EXT = /\.(pdf|docx?|odt|rtf|txt|md|json|xml|html?)$/i;

/** The file-type icon: by mime family first, the extension when the mime is generic. */
export function fileIcon(name: string, mime: string): LucideIcon {
  if (mime.startsWith('audio/')) return FileAudio;
  if (ARCHIVE_EXT.test(name) || /zip|compressed|x-tar/.test(mime)) return FileArchive;
  if (SHEET_EXT.test(name) || /spreadsheet|csv|excel/.test(mime)) return FileSpreadsheet;
  if (TEXT_EXT.test(name) || mime.startsWith('text/') || mime === 'application/pdf') return FileText;
  return File;
}

interface FileChipProps {
  name: string;
  mime: string;
  status?: PendingFile['status'];
  progress?: number;
}

/** One square file chip: type icon, name truncated under it, an upload bar
 * while uploading, a warning mark when the upload failed. */
export function FileChip({ name, mime, status = 'done', progress = 1 }: FileChipProps) {
  const Icon = status === 'error' ? AlertTriangle : fileIcon(name, mime);
  return (
    <div className={`relative w-16 h-16 border flex flex-col items-center justify-center gap-1 px-1 bg-surface2 ${
      status === 'error' ? 'border-red' : 'border-border'}`}>
      <Icon size={22} className={status === 'error' ? 'text-red' : 'text-text-muted'} />
      <span className="w-full truncate text-center text-ui-2xs text-text-muted">{name}</span>
      {status === 'uploading' && (
        <div className="absolute left-0 right-0 bottom-0 h-1 bg-black/20">
          <div className="h-full bg-accent" style={{ width: `${Math.round(progress * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

/** The hover/touch remove overlay shared by both chip kinds. */
function RemoveOverlay() {
  return (
    <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity">
      <div className="w-10 h-10 bg-black/50 flex items-center justify-center">
        <X size={20} className="text-white" />
      </div>
    </div>
  );
}

export function AttachmentChips({ images, onRemove, imageMeta = {}, pressing = 0, files = [], onRemoveFile }: Props) {
  const { t } = useTranslation();
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  if (images.length === 0 && files.length === 0 && pressing === 0) return null;
  // WHY: compared by area, not by side — an EXIF rotation swaps the sides of an
  // image that was not compressed at all.
  const sizeLabel = (src: string): string | null => {
    const m = imageMeta[src];
    if (!m) return null;
    const size = `${m.width}×${m.height}`;
    return m.width * m.height === m.sourceWidth * m.sourceHeight
      ? size
      : t('imageSizePressed', { size, source: `${m.sourceWidth}×${m.sourceHeight}` });
  };
  const open = openIndex !== null && openIndex < images.length ? openIndex : null;
  return (
    <div className="flex gap-2 mb-2 flex-wrap">
      {images.map((img, i) => (
        <div
          key={i}
          className="group relative w-16 h-16 border border-border cursor-pointer"
          onClick={() => setOpenIndex(i)}
          title={sizeLabel(img) ?? t('attachmentImage')}
        >
          <img src={img} alt="" className="w-full h-full object-cover" />
          <div className="absolute top-0 right-0 bg-surface opacity-0 group-hover:opacity-100 focus-within:opacity-100 [@media(hover:none)]:opacity-100">
            <IconButton
              size="sm"
              title={t('removeImage')}
              aria-label={t('removeImage')}
              onClick={(e: React.MouseEvent) => { e.stopPropagation(); onRemove(i); }}
            >
              <X size={14} />
            </IconButton>
          </div>
        </div>
      ))}
      {Array.from({ length: pressing }, (_, i) => (
        <div
          key={`pressing-${i}`}
          className="w-16 h-16 border border-border bg-surface2 flex items-center justify-center"
          title={t('chatImagePressing')}
          data-testid="pressing-image-chip"
        >
          <Loader2 size={20} className="text-text-muted animate-spin" />
        </div>
      ))}
      {files.map(f => (
        <div
          key={f.key}
          className="group relative cursor-pointer"
          onClick={() => onRemoveFile?.(f.key)}
          title={`${f.name} — ${t('removeFile')}`}
          data-testid="pending-file-chip"
        >
          <FileChip name={f.name} mime={f.mime} status={f.status} progress={f.progress} />
          <RemoveOverlay />
        </div>
      ))}
      {open !== null && (
        <ImageLightbox
          srcs={images}
          index={open}
          onIndexChange={setOpenIndex}
          onClose={() => setOpenIndex(null)}
          title={sizeLabel(images[open]) ?? t('attachmentImage')}
          actualSize
        />
      )}
    </div>
  );
}
