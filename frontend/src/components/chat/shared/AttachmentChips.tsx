/** Shared pending-attachment strip: image thumbnails and file chips, one square
 * size. Click a chip to remove it. Used by both AI and notes composers (files:
 * AI only). `FileChip` alone also renders a sent message's files. */
// SYSTEM: chat-composer — shared composer attachment chips
// INVARIANT: no rounded corners anywhere (project rule) — the hover overlay uses
// a square backdrop, not rounded-full. Why: the AI composer historically used
// rounded-full here; unifying on the note style keeps the whole app square.

import { AlertTriangle, File, FileArchive, FileAudio, FileSpreadsheet, FileText, X } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useTranslation } from '../../../i18n';
import type { PendingFile } from '../../../store/chat-store/types';

interface Props {
  images: string[];
  onRemove: (index: number) => void;
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

export function AttachmentChips({ images, onRemove, files = [], onRemoveFile }: Props) {
  const { t } = useTranslation();
  if (images.length === 0 && files.length === 0) return null;
  return (
    <div className="flex gap-2 mb-2 flex-wrap">
      {images.map((img, i) => (
        <div
          key={i}
          className="group relative w-16 h-16 border border-border cursor-pointer"
          onClick={() => onRemove(i)}
          title={t('removeImage')}
        >
          <img src={img} alt="" className="w-full h-full object-cover" />
          <RemoveOverlay />
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
    </div>
  );
}
