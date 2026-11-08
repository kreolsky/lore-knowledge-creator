/**
 * useImageDropHandlers — shared drag-drop image capture for the right-panel
 * chat surfaces (AI chat + note chat).
 *
 * Images go through the shared attachImage, so the chat and note drop paths
 * cannot diverge: each file is pressed and budget-checked on its own (per-file
 * partial-accept).
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
import { attachImage, type AttachSurface } from '../chat/attach-image';
import { CHAT_FILE_MAX_MB, fileRefusal } from '../chat/file-attachment-policy';

interface UseImageDropHandlersOptions {
  /** The store side of an image attach (press, budget, chip). */
  surface: AttachSurface;
  /** Takes one accepted non-image file; absent = this surface takes images only. */
  addFile?: (file: File) => void;
  /** When false, drops are still swallowed but no images are enqueued. */
  enabled: boolean;
}

export function useImageDropHandlers({ surface, addFile, enabled }: UseImageDropHandlersOptions) {
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
    const { showToast } = useAppStore.getState();
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
      void attachImage(file, surface);
    }
    if (imagesOnlyRefused) showToast(t('chatFileImagesOnly'), 'error');
  }, [enabled, surface, addFile, t]);

  return {
    isDragging,
    dragHandlers: { onDragEnter, onDragLeave, onDragOver, onDrop },
  };
}
