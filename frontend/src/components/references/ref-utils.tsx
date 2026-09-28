/** Shared utilities, constants, and small presentational components for reference cards. */

import { Mic, Image, FileText, FileArchive, Loader } from 'lucide-react';
import { Reference, Project, Document } from '../../types';
import { useTranslation } from '../../i18n';
import type { ExportFormat } from '../../hooks/useDocumentExport';
import type { OriginalFile } from '../ui/DownloadMenu';

/** Converted PDF: a markdown ref carrying the original as file_path. */
export function isConvertedPdf(reference: Reference): boolean {
  return reference.media_type === 'markdown' && !!reference.file_path && reference.file_path.toLowerCase().endsWith('.pdf');
}

/**
 * DownloadMenu inputs for a reference — ONE derivation shared by the center-mode banner
 * and the right-panel plaque. `original` (the uploaded file) is offered ONLY for
 * markdown refs with a file_path (converted PDF / imported .docx) — they have no media
 * surface to host the action. Audio/image/file refs download their original ON the
 * media (MediaFileActions), so the menu carries text formats only. A converted PDF
 * drops the pdf export item: its `original` already IS the .pdf, and a pdf rebuilt from
 * the recognized text is the wrong file to hand back.
 */
export function referenceDownloadProps(reference: Reference): { original?: OriginalFile; exportFormats?: ExportFormat[] } {
  const originalName = reference.file_meta?.original_name || reference.title;
  const originalExt = originalName.includes('.') ? originalName.slice(originalName.lastIndexOf('.')) : '';
  const original = reference.file_path && reference.media_type === 'markdown'
    ? { url: `/api/files/${reference.reference_id}/${reference.file_meta?.original_name || 'file'}`, ext: originalExt, name: originalName }
    : undefined;
  return { original, exportFormats: isConvertedPdf(reference) ? ['docx', 'md'] : undefined };
}

export function createTempRef(project: Project, document: Document, fileName: string, mediaType: 'audio' | 'image' | 'markdown'): { tempId: string; tempRef: Reference } {
  const tempId = `temp_${Date.now()}_${fileName}`;
  return {
    tempId,
    tempRef: {
      reference_id: tempId,
      project_id: project.project_id,
      document_id: document.document_id,
      title: fileName,
      media_type: mediaType,
      processing_status: 'uploading',
      source_url: null, content: '', file_path: null, file_meta: null,
      updated_at: new Date().toISOString(), created_at: new Date().toISOString(),
    },
  };
}

export function hueForId(id: string) {
  let hash = 0;
  for (let i = 0; i < id.length; i++) {
    hash = id.charCodeAt(i) + ((hash << 5) - hash);
  }
  return Math.abs(hash) % 360;
}

// Active state of the archive / split / panel toggles (ReferencesPanel toolbar) and the
// always-pressed eye on RefPanelPlaque: the same accent-on-white as Button variant="primary"
// (the chat send button), so the chosen mode is unmistakable. Only these carry it — every
// other toolbar button keeps the gray hover.
export const ACTIVE_TOGGLE_CLS = 'bg-accent! text-white! hover:bg-accent! hover:text-white!';

export const AUDIO_EXTS = ['.webm', '.ogg', '.opus', '.mp3', '.wav', '.m4a', '.mp4', '.aac', '.3gp'];
export const IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp'];
export const MEDIA_EXTS = [...AUDIO_EXTS, ...IMAGE_EXTS];
// Delimited-text tables — imported into a native Lore table (see csv-table.ts). No file is
// stored: the data is fully absorbed into the table model (a "virtual reference" badge).
export const TABLE_EXTS = ['.csv', '.tsv'];

export const MAX_AUDIO_SIZE_MB = 500;
export const MAX_IMAGE_SIZE_MB = 50;
export const MAX_DOCX_SIZE_MB = 25;
export const MAX_PDF_SIZE_MB = 50;
export const MAX_MARKDOWN_SIZE_MB = 50;

export function isMediaFile(name: string): boolean {
  const lower = name.toLowerCase();
  return MEDIA_EXTS.some(ext => lower.endsWith(ext));
}

// .docx only — legacy .doc is out of scope (Pandoc cannot read it without LibreOffice).
export function isDocxFile(name: string): boolean {
  return name.toLowerCase().endsWith('.docx');
}

// .pdf — converted to markdown by the converter service (pymupdf4llm), mirroring .docx.
export function isPdfFile(name: string): boolean {
  return name.toLowerCase().endsWith('.pdf');
}

// .csv/.tsv — imported locally into a table model (no upload round-trip, no stored file).
// xlsx/xls are out of scope for this iteration.
export function isTableFile(name: string): boolean {
  const lower = name.toLowerCase();
  return TABLE_EXTS.some(ext => lower.endsWith(ext));
}

export function isAudioFile(name: string): boolean {
  const lower = name.toLowerCase();
  return AUDIO_EXTS.some(ext => lower.endsWith(ext));
}

export function getMediaType(name: string): 'audio' | 'image' | 'markdown' {
  const lower = name.toLowerCase();
  if (AUDIO_EXTS.some(ext => lower.endsWith(ext))) return 'audio';
  if (IMAGE_EXTS.some(ext => lower.endsWith(ext))) return 'image';
  return 'markdown';
}

export function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Short group label for a reference's media_type — used as the ref-list section
// header (txt / audio / img) and the inline "type | parent" meta prefix.
const MEDIA_TYPE_LABEL: Record<Reference['media_type'], string> = {
  markdown: 'txt',
  audio: 'audio',
  image: 'img',
  file: 'file',
};

export function mediaTypeLabel(mediaType: Reference['media_type']): string {
  return MEDIA_TYPE_LABEL[mediaType];
}

export function RefIcon({ item }: { item: Reference }) {
  if (item.media_type === 'audio') return <Mic size={14} />;
  if (item.media_type === 'image') return <Image size={14} />;
  if (item.media_type === 'file') return <FileArchive size={14} />;
  return <FileText size={14} />;
}

export function StatusBadge({ item }: { item: Reference }) {
  const { t } = useTranslation();
  if (!item.processing_status) return null;

  const styles: Record<string, { color: string; bg: string; label: string }> = {
    uploading:  { color: 'var(--accent)',  bg: 'var(--accent-soft)',                       label: t('statusUploading') },
    queued:     { color: 'var(--text-dim)', bg: 'var(--surface3)',                          label: t('statusQueued') },
    processing: { color: 'var(--amber)',   bg: 'var(--amber-soft, rgba(245,158,11,0.12))', label: t('statusProcessing') },
    ready:      { color: 'var(--green)',   bg: 'var(--green-soft, rgba(34,197,94,0.12))',  label: item.file_meta?.duration_sec ? formatDuration(item.file_meta.duration_sec) : '' },
    error:      { color: 'var(--red)',     bg: 'var(--red-soft, rgba(239,68,68,0.12))',    label: t('statusError') },
  };
  const s = styles[item.processing_status];
  if (!s || !s.label) return null;

  const showSpinner = item.processing_status === 'uploading'
    || item.processing_status === 'queued'
    || item.processing_status === 'processing';

  return (
    <span
      className="text-ui-2xs font-semibold py-px px-1.5 inline-flex items-center gap-[3px]"
      style={{ color: s.color, background: s.bg }}
    >
      {showSpinner && <Loader size={9} className="spin" />}
      {s.label}
    </span>
  );
}
