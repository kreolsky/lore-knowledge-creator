/**
 * useImageDropHandlers — shared drag-drop image capture for the right-panel
 * chat surfaces (AI chat + note chat).
 *
 * Owns the single attachment-budget policy (per-file partial-accept, matching
 * ChatPanel) and routes through the shared `attachmentBudgetExceeded` /
 * `formatAttachmentMb` helpers so the chat and note drop paths cannot diverge.
 * `maxAttachmentMb` is read from app-store (hydrated from /chat/models) — the
 * single source for both surfaces.
 *
 * The drop is ALWAYS preventDefault'd + stopPropagation'd so a file dropped on a
 * chat/note tab never falls through to the references upload path or the
 * browser's open-file default. Images are only enqueued when `enabled` is true
 * (AI chat: always; note chat: only when a thread is open).
 *
 * Any other file goes to `addFile` when the surface takes files (the AI chat,
 * full access — it uploads into the sandbox workspace); everywhere else it is
 * refused with a toast, never skipped silently.
 */
import { useState, useRef, useCallback } from 'react';
import type React from 'react';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import { totalAttachmentBytes, attachmentBudgetExceeded, formatAttachmentMb } from '../utils/attachment-size';
import { CHAT_FILE_MAX_MB, fileRefusal } from '../chat/file-attachment-policy';

interface UseImageDropHandlersOptions {
  /** Returns the currently-queued pending image data URLs (budget baseline). */
  getPending: () => string[];
  /** Enqueues one accepted image (data URL). */
  addImage: (dataUrl: string) => void;
  /** Takes one accepted non-image file; absent = this surface takes images only. */
  addFile?: (file: File) => void;
  /** When false, drops are still swallowed but no images are enqueued. */
  enabled: boolean;
}

export function useImageDropHandlers({ getPending, addImage, addFile, enabled }: UseImageDropHandlersOptions) {
  const { t } = useTranslation();
  const [isDragging, setIsDragging] = useState(false);
  const dragCounter = useRef(0);

  const onDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    if (!e.dataTransfer.types.includes('Files')) return;
    dragCounter.current++;
    if (enabled) setIsDragging(true);
  }, [enabled]);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragCounter.current--;
    if (dragCounter.current === 0) setIsDragging(false);
  }, []);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounter.current = 0;
    setIsDragging(false);
    if (!enabled) return;
    const { maxAttachmentMb: maxMb, showToast } = useAppStore.getState();
    let projected = totalAttachmentBytes(getPending());
    let imagesOnlyRefused = false;
    for (const file of e.dataTransfer.files) {
      if (!file.type.startsWith('image/')) {
        if (!addFile) {
          imagesOnlyRefused = true;
          continue;
        }
        const refusal = fileRefusal(file);
        if (refusal === 'size') showToast(t('chatFileTooLarge', { name: file.name, limit: CHAT_FILE_MAX_MB }), 'error');
        else if (refusal === 'type') showToast(t('chatFileTypeRefused', { name: file.name }), 'error');
        else addFile(file);
        continue;
      }
      if (attachmentBudgetExceeded(projected, file.size, maxMb)) {
        showToast(
          t('attachmentsExceedLimit', { used: formatAttachmentMb(projected + file.size), limit: maxMb }),
          'error',
        );
        continue; // per-file partial-accept: skip the offender, keep the rest
      }
      projected += file.size;
      const reader = new FileReader();
      reader.onload = () => {
        if (typeof reader.result === 'string') addImage(reader.result);
      };
      reader.readAsDataURL(file);
    }
    if (imagesOnlyRefused) showToast(t('chatFileImagesOnly'), 'error');
  }, [enabled, getPending, addImage, addFile, t]);

  return {
    isDragging,
    dragHandlers: { onDragEnter, onDragLeave, onDragOver, onDrop },
  };
}
