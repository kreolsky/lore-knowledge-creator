/** File upload, drag-drop, and transcription polling for references. */
// ARCH: Extracted from ReferencesPanel — all upload I/O and polling in one hook.
// INVARIANT: optimistic UI — temp refs appear instantly, replaced on server response.  Why: temp refs appear instantly (optimistic) and are replaced on server response, so upload feels immediate; rollback on failure.

import { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import type React from 'react';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { apiClient } from '../api/client';
import { uploadReferenceFile } from '../api/reference-upload';
import { tierRefusal } from '../utils/server-refusal-detail';
import { useTranslation } from '../i18n';
import type { TranslationKey } from '../i18n/en';
import type { Reference } from '../types';
import { getEntityHandle } from '../collab/active-handle-registry';
import { createUnlinkedTable } from '../components/editor/live-preview/table-document-ops';
import { readCsvForImport, MAX_TABLE_IMPORT_ROWS, MAX_TABLE_IMPORT_COLS, MAX_TABLE_IMPORT_CELLS } from '../components/editor/live-preview/csv-table';
import {
  createTempRef,
  isMediaFile,
  isAudioFile,
  isDocxFile,
  isPdfFile,
  isTableFile,
  getMediaType,
  MAX_AUDIO_SIZE_MB,
  MAX_IMAGE_SIZE_MB,
  MAX_DOCX_SIZE_MB,
  MAX_PDF_SIZE_MB,
  MAX_MARKDOWN_SIZE_MB,
} from '../components/references/ref-utils';

type ImportSpec = {
  endpoint: string;
  maxMB: number;
  tooLargeKey: TranslationKey;
  failedKey: TranslationKey;
  sendsTitle: boolean;
  withImageRefs: boolean;
};

const IMPORT_SPECS = {
  markdown: { endpoint: '/references/upload-markdown', maxMB: MAX_MARKDOWN_SIZE_MB, tooLargeKey: 'fileTooLargeMarkdown', failedKey: 'failedToImportMarkdown', sendsTitle: false, withImageRefs: true },
  docx: { endpoint: '/references/upload-docx', maxMB: MAX_DOCX_SIZE_MB, tooLargeKey: 'fileTooLargeDocx', failedKey: 'failedToImportDocx', sendsTitle: true, withImageRefs: false },
  pdf: { endpoint: '/references/upload-pdf', maxMB: MAX_PDF_SIZE_MB, tooLargeKey: 'fileTooLargePdf', failedKey: 'failedToImportPdf', sendsTitle: true, withImageRefs: false },
} satisfies Record<string, ImportSpec>;

export function useReferenceUpload() {
  const currentProject = useAppStore(s => s.currentProject);
  const currentDocument = useAppStore(s => s.currentDocument);
  const addReference = useAppStore(s => s.addReference);
  const updateReference = useAppStore(s => s.updateReference);
  const replaceReference = useAppStore(s => s.replaceReference);
  const addPendingUploadRefIds = useAppStore(s => s.addPendingUploadRefIds);
  const removePendingUploadRefIds = useAppStore(s => s.removePendingUploadRefIds);
  const references = useAppStore(s => s.references);
  const isPublicShare = useUIStore(s => s.isPublicShare);
  const { t } = useTranslation();

  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const pendingIds = useMemo(
    () => references
      .filter(r => r.processing_status === 'queued' || r.processing_status === 'processing')
      .map(r => r.reference_id)
      .join(','),
    [references],
  );

  // INVARIANT(security): never poll on the anonymous /s/:token surface.
  // Why: /references/{id}/status is auth-gated (401); apiClient turns 401 into a
  // redirect to '/', kicking the visitor off the share page after ~3s. Public shares
  // are read-only snapshots — no writes are possible, so polling is meaningless there.
  useEffect(() => {
    if (!pendingIds || isPublicShare) return;
    const ids = pendingIds.split(',');
    let delay = 3000;
    let timer: ReturnType<typeof setTimeout>;

    const poll = async () => {
      let hasError = false;
      await Promise.all(ids.map(async (refId) => {
        try {
          const ref = references.find(r => r.reference_id === refId);
          if (!ref) return;
          const status = await apiClient.get(`/references/${refId}/status`);
          if (status.processing_status !== ref.processing_status) {
            updateReference(refId, {
              processing_status: status.processing_status,
              content: status.content || ref.content,
              media_type: status.media_type || ref.media_type,
            });
          }
        } catch (err: any) {
          hasError = true;
          if (err?.status === 401 || err?.status === 403) {
            useAppStore.getState().showToast(t('failedToPollReference'), 'error');
          }
        }
      }));
      if (hasError) {
        delay = Math.min(delay * 2, 30_000);
      } else {
        delay = 3000;
      }
      timer = setTimeout(poll, delay);
    };

    timer = setTimeout(poll, delay);
    return () => clearTimeout(timer);
  }, [pendingIds, isPublicShare]);

  const handleFileUpload = useCallback(async (file: File) => {
    if (!currentProject || !currentDocument) return;

    const isAudio = isAudioFile(file.name);
    const maxMB = isAudio ? MAX_AUDIO_SIZE_MB : MAX_IMAGE_SIZE_MB;
    if (file.size > maxMB * 1024 * 1024) {
      const key = isAudio ? 'fileTooLargeAudio' : 'fileTooLargeImage';
      useAppStore.getState().showToast(t(key, { limit: String(maxMB) }));
      return;
    }

    const { tempId, tempRef } = createTempRef(
      currentProject, currentDocument, file.name, getMediaType(file.name),
    );
    addReference(tempRef);

    try {
      const ref = await uploadReferenceFile(currentProject.project_id, currentDocument.document_id, file);
      replaceReference(tempId, ref);
    } catch (err) {
      // see SYSTEM: capability-tiers — a dragged-in audio file from a non-voice
      // account: name the account-level reason and drop the placeholder (the
      // server created no reference, so an "error" row would be a phantom).
      if (tierRefusal(err) !== null) {
        useAppStore.getState().removeReference(tempId);
        useAppStore.getState().showToast(t('tierVoiceRequired'), 'error');
        return;
      }
      console.error('Failed to upload file', err);
      useAppStore.getState().showToast(t('failedToUpload'), 'error');
      updateReference(tempId, { processing_status: 'error' });
    }
  }, [currentProject, currentDocument]);

  // Text import (any non-media, non-docx/pdf/table file — backend validates by content,
  // not extension) and DOCX/PDF imports differ only in this spec. DOCX/PDF are async: the
  // endpoint returns a queued markdown reference, the worker converts it via the converter
  // service, the status-polling effect picks up the ready content, and extracted image refs
  // arrive via the reference_created WS event. Markdown returns its image refs inline.
  const importToReference = useCallback(async (file: File, spec: ImportSpec) => {
    if (!currentProject || !currentDocument) return;
    if (file.size > spec.maxMB * 1024 * 1024) {
      useAppStore.getState().showToast(t(spec.tooLargeKey, { limit: String(spec.maxMB) }));
      return;
    }
    const { tempId, tempRef } = createTempRef(currentProject, currentDocument, file.name, 'markdown');
    addReference(tempRef);
    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('project_id', currentProject.project_id);
      formData.append('document_id', currentDocument.document_id);
      // WHY: markdown omits the title so the backend derives it from the file stem.
      if (spec.sendsTitle) formData.append('title', file.name);
      const result = await apiClient.upload(spec.endpoint, formData);
      if (!spec.withImageRefs) {
        replaceReference(tempId, result);
        return;
      }
      const allIds = [result.reference.reference_id, ...result.image_references.map((r: Reference) => r.reference_id)];
      addPendingUploadRefIds(allIds);
      replaceReference(tempId, result.reference);
      for (const ref of result.image_references) {
        addReference(ref);
      }
      setTimeout(() => removePendingUploadRefIds(allIds), 5000);
    } catch (err) {
      console.error(`Failed to import ${spec.endpoint}`, err);
      useAppStore.getState().showToast(t(spec.failedKey), 'error');
      updateReference(tempId, { processing_status: 'error' });
    }
  }, [currentProject, currentDocument, addReference, replaceReference, updateReference, addPendingUploadRefIds, removePendingUploadRefIds, t]);

  // CSV/TSV → native table. Parsed fully client-side into a table model: no upload
  // round-trip, no stored file (a "virtual reference" badge appears reactively via
  // useDocumentTables). ARCH: A' — unlinked table in the current document's ydoc.
  const handleTableImport = useCallback(async (file: File) => {
    // Guard the handler, not just the UI: a CSV import is a LOCAL-only ydoc write (no
    // backend to reject it), and the panel's dragHandlers are spread unconditionally — a
    // viewer/public-share drop must not mutate the doc (mirrors handleTableDelete + the
    // editor's getDropGuard; see CLAUDE.md "never rely on UI hiding alone").
    if (isPublicShare || useAppStore.getState().accessLevel !== 'full') return;
    // Identity-scoped: the CURRENT DOCUMENT's entity ydoc — never the focused slot
    // (with a reference focused the slot holds the ref's handle and the imported table
    // would land in the wrong ydoc; the badge list is entity-scoped too, so a slot
    // import would be a silent no-op: import "succeeds", badge never appears).
    const ydoc = currentDocument ? getEntityHandle(currentDocument.document_id)?.ydoc : undefined;
    if (!ydoc) {
      useAppStore.getState().showToast(t('csvImportFailed'), 'error');
      return;
    }
    if (file.size > MAX_MARKDOWN_SIZE_MB * 1024 * 1024) {
      useAppStore.getState().showToast(t('fileTooLargeMarkdown', { limit: String(MAX_MARKDOWN_SIZE_MB) }));
      return;
    }
    try {
      const read = await readCsvForImport(file);
      if ('refusal' in read) {
        useAppStore.getState().showToast(
          t('csvTooLarge', { rows: String(MAX_TABLE_IMPORT_ROWS), cols: String(MAX_TABLE_IMPORT_COLS), cells: String(MAX_TABLE_IMPORT_CELLS) }),
          'error',
        );
        return;
      }
      const { matrix } = read;
      const stem = file.name.replace(/\.(csv|tsv)$/i, '');
      createUnlinkedTable(ydoc, matrix, stem);
      // No temp ref / no optimistic card — a table is not a reference; the badge appears
      // reactively via useDocumentTables observing the ydoc `tables` subtree.
    } catch (err) {
      console.error('Failed to import CSV/TSV table', err);
      useAppStore.getState().showToast(t('csvImportFailed'), 'error');
    }
  }, [t, isPublicShare, currentDocument]);

  const handlerFor = useCallback((file: File): ((f: File) => Promise<void>) => {
    if (isMediaFile(file.name)) return handleFileUpload;
    if (isTableFile(file.name)) return handleTableImport;
    const spec = isDocxFile(file.name) ? IMPORT_SPECS.docx : isPdfFile(file.name) ? IMPORT_SPECS.pdf : IMPORT_SPECS.markdown;
    return f => importToReference(f, spec);
  }, [handleFileUpload, handleTableImport, importToReference]);

  const handleFileInput = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file || !currentProject || !currentDocument) return;
    await handlerFor(file)(file);
    e.target.value = '';
  }, [currentProject, currentDocument, handlerFor]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback(async (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (!currentProject || !currentDocument) return;

    for (const file of Array.from(e.dataTransfer.files)) {
      await handlerFor(file)(file);
    }
  }, [currentProject, currentDocument, handlerFor]);

  return {
    isDragging,
    fileInputRef,
    handleFileInput,
    dragHandlers: {
      onDragOver: handleDragOver,
      onDragLeave: handleDragLeave,
      onDrop: handleDrop,
    },
  };
}
