/**
 * File-drop import for the document editor (drag an image/.docx/text file into
 * the body).
 *
 * Owns the pending-drop/importing state, the drop type-guard, and the
 * confirm→upload→insert flow. Every dropped file lands AT the drop position
 * (dropInsertSpec): image embeds inline, block content (table anchor,
 * imported markdown) on its own line. An IMAGE dropped over a table cell
 * targets the CELL's editor (editorDropPoint mounts a lazy cell) at its drawn
 * width (dropEmbedForView); .docx/text/CSV always target the HOST document —
 * a table inside a cell is not a thing. Editor.tsx keeps computing the
 * editable gate (it lives with the collab state) and passes it in via a
 * ref-read getter.
 */
// SYSTEM: editor — file-drop import (drag an image/.docx/text file into the body).

import { useState, useRef, useCallback, useMemo } from 'react';
import type React from 'react';
import type { EditorView } from '@codemirror/view';
import { makeFileDropExtension, type FileDropCoords } from '../../editor/file-drop-handler';
import { useTranslation } from '../../i18n';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import { uploadReferenceFile } from '../../api/reference-upload';
import type { Document, Reference } from '../../types';
import { entityYdoc, getViewEntity } from '../../editor/active-editor';
import { dropEmbedForView, dropInsertSpec, editorDropPoint, spliceAt } from './drop-target';
import { createTable, tableAnchor, normalizeTableLabel } from '../editor/live-preview/table-block-model';
import { sniffDelimiter, parseCsv, MAX_TABLE_IMPORT_ROWS, MAX_TABLE_IMPORT_COLS, MAX_TABLE_IMPORT_CELLS } from '../editor/live-preview/csv-table';
import { createTempRef, isImageFile, isTableFile, MAX_IMAGE_SIZE_MB, MAX_MARKDOWN_SIZE_MB } from '../references/ref-utils';

export interface DropGuard {
  isReadonly: boolean;
  editable: boolean;
}

export interface UseEditorFileDropParams {
  editorViewRef: React.RefObject<EditorView | null>;
  activeItemRef: React.RefObject<Document | Reference | null>;
  /** Live editable gate — read at drop time (the CM6 extension is created once). */
  getDropGuard: () => DropGuard;
}

export interface FileDropModalProps {
  pendingDrop: { file: File; pos: number } | null;
  importing: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}

export function useEditorFileDrop({
  editorViewRef,
  activeItemRef,
  getDropGuard,
}: UseEditorFileDropParams): { fileDropExtension: ReturnType<typeof makeFileDropExtension>; modalProps: FileDropModalProps } {
  const { t } = useTranslation();

  // Pending state: holds the file + drop position between the CM6 drop handler
  // (which can't open a modal) and the confirmation modal's confirm action.
  const [pendingDrop, setPendingDrop] = useState<{ file: File; pos: number } | null>(null);
  const [importing, setImporting] = useState(false);
  const pendingDropRef = useRef(pendingDrop);
  pendingDropRef.current = pendingDrop;

  // CSV/TSV → inline table at the drop position. Parsed locally (no upload modal, no
  // round-trip); creates the table model + splices the `![label](table:id)` anchor at
  // the drop position on its own line (dropInsertSpec; mirrors paste's
  // importGfmTables). System: table-block.
  const handleTableInsert = useCallback(async (file: File, pos: number) => {
    const view = editorViewRef.current;
    // ARCH (split view): the table lands in the DROP VIEW'S entity ydoc — the focused
    // slot may hold the other column's handle. Unknown views keep the slot fallback
    // (parity with paste-handler's insertVerbatim targeting).
    // WHY: a KNOWN entity whose handle has not landed must fail loudly here (toast),
    // never import into the slot's foreign ydoc — model and anchor would land in
    // different ydocs.
    const ydoc = view ? entityYdoc(view) : undefined;
    if (!view || !ydoc) {
      useAppStore.getState().showToast(t('csvImportFailed'), 'error');
      return;
    }
    if (file.size > MAX_MARKDOWN_SIZE_MB * 1024 * 1024) {
      useAppStore.getState().showToast(t('fileTooLargeMarkdown', { limit: String(MAX_MARKDOWN_SIZE_MB) }));
      return;
    }
    try {
      const text = await file.text();
      const lower = file.name.toLowerCase();
      const delimiter = lower.endsWith('.tsv') ? '\t' : sniffDelimiter(text, ',');
      const matrix = parseCsv(text, delimiter);
      const rows = matrix.length;
      const cols = matrix.reduce((m, r) => Math.max(m, r.length), 0);
      if (rows === 0 || rows > MAX_TABLE_IMPORT_ROWS || cols > MAX_TABLE_IMPORT_COLS || rows * cols > MAX_TABLE_IMPORT_CELLS) {
        useAppStore.getState().showToast(
          t('csvTooLarge', { rows: String(MAX_TABLE_IMPORT_ROWS), cols: String(MAX_TABLE_IMPORT_COLS), cells: String(MAX_TABLE_IMPORT_CELLS) }),
          'error',
        );
        return;
      }
      const label = normalizeTableLabel(file.name.replace(/\.(csv|tsv)$/i, ''));
      const id = createTable(ydoc, matrix);
      const anchor = tableAnchor(label, id);
      spliceAt(view, dropInsertSpec(view.state.doc, pos, anchor, 'block'));
    } catch (err) {
      console.error('CSV/TSV table drop failed', err);
      useAppStore.getState().showToast(t('csvImportFailed'), 'error');
    }
  }, [t]);
  const handleTableInsertRef = useRef(handleTableInsert);
  handleTableInsertRef.current = handleTableInsert;

  // Image drop: upload as an image reference hosted by the DROP VIEW'S document,
  // then insert the `![title](ref:id)` embed INLINE at the drop position. No
  // confirmation modal — the drop itself is the intent.
  // ARCH (split view): same identity rule as the table path — the host is the
  // drop view's entity, never the focused slot's item (a table cell is
  // registered to its host's entity at mount). A drop onto an open REFERENCE
  // (split column or center) is refused with a toast: a reference cannot host
  // references.
  const handleImageDrop = useCallback(async (file: File, view: EditorView, pos: number) => {
    const store = useAppStore.getState();
    // Resolve the drop view's item: the registered entity is a document_id or a
    // reference_id; an unregistered view falls back to the slot item (the same
    // fallback scope as the table path).
    const entityId = getViewEntity(view);
    const host: Document | Reference | null = entityId
      ? store.documents.find(d => d.document_id === entityId)
        ?? store.references.find(r => r.reference_id === entityId)
        ?? null
      : activeItemRef.current;
    if (!host || 'reference_id' in host) {
      store.showToast(t('dropImageIntoReferenceUnsupported'), 'error');
      return;
    }
    const project = store.currentProject;
    // A missing/mismatched project means a switch race — fail loudly rather
    // than parent the reference into another project's document.
    if (!project || project.project_id !== host.project_id) {
      store.showToast(t('failedToUpload'), 'error');
      return;
    }
    if (file.size > MAX_IMAGE_SIZE_MB * 1024 * 1024) {
      store.showToast(t('fileTooLargeImage', { limit: String(MAX_IMAGE_SIZE_MB) }));
      return;
    }
    const { tempId, tempRef } = createTempRef(project, host, file.name, 'image');
    store.addReference(tempRef);
    try {
      const ref = await uploadReferenceFile(project.project_id, host.document_id, file);
      // WHY: insert the embed only AFTER the reference is in the store —
      // ImageWidget resolves `ref:id` through the references list
      // (transcludeMap), so inserting first would flash the raw `![…](ref:…)`
      // text for a frame.
      store.replaceReference(tempId, ref);
      // The drop view may be gone after the async upload (a cell torn down by a
      // rebuild): never dispatch into it — the reference stays, the toast says so.
      if (!view.dom.isConnected) {
        store.showToast(t('failedToUpload'), 'error');
        return;
      }
      const embed = dropEmbedForView(view, `![${file.name}](ref:${ref.reference_id})`, true, ref.file_meta?.width);
      spliceAt(view, dropInsertSpec(view.state.doc, pos, embed, 'inline'));
    } catch (err) {
      console.error('image drop upload failed', err);
      store.showToast(t('failedToUpload'), 'error');
      store.updateReference(tempId, { processing_status: 'error' });
    }
  }, [t]);
  const handleImageDropRef = useRef(handleImageDrop);
  handleImageDropRef.current = handleImageDrop;

  // INVARIANT: client-side type guard is permissive — final validation is  Why: the client guard is permissive (rejects only obvious binary) to avoid a useless round-trip; final validation is server-side via is_text_bytes.
  // server-side via is_text_bytes. We only reject obvious binary (not docx and
  // not text-ish MIME) to avoid a useless upload round-trip. CSV/TSV is handled
  // by a dedicated local-parse path (no modal).
  const handleEditorFileDrop = useCallback((file: File, coords: FileDropCoords) => {
    const { isReadonly: ro, editable: ed } = getDropGuard();
    if (ro || !ed) return; // readonly/offline viewers don't insert
    const name = file.name.toLowerCase();
    if (isTableFile(name)) {
      void handleTableInsertRef.current(file, coords.hostPos); // fire-and-forget; toasts handle errors
      return;
    }
    if (file.type.startsWith('image/') || isImageFile(name)) {
      // Routed AFTER the guard, so a read-only drop mounts no cell; no editor
      // under the pointer ⇒ the host view at hostPos.
      const host = editorViewRef.current;
      const target = editorDropPoint(coords.x, coords.y)
        ?? (host ? { view: host, pos: coords.hostPos } : null);
      if (target) void handleImageDropRef.current(file, target.view, target.pos); // fire-and-forget; toasts handle errors
      return;
    }
    const isDocx = name.endsWith('.docx');
    const mt = file.type;
    const looksText = mt.startsWith('text/')
      || mt === 'application/json' || mt === 'application/xml' || mt === '';
    if (!isDocx && !looksText) {
      useAppStore.getState().showToast(
        t('unsupportedFileType', { name: file.name }), 'error',
      );
      return;
    }
    // .docx/text always lands in the HOST view — a table inside a cell is not a thing.
    setPendingDrop({ file, pos: coords.hostPos });
  }, [t, getDropGuard]);
  const handleEditorFileDropRef = useRef(handleEditorFileDrop);
  handleEditorFileDropRef.current = handleEditorFileDrop;

  // Confirm → upload → insert at the drop position (block: own line per
  // dropInsertSpec).
  const handleConfirmImport = useCallback(async () => {
    const drop = pendingDropRef.current;
    const item = activeItemRef.current;
    const view = editorViewRef.current;
    if (!drop || !item || !view) return;
    const projectId = 'reference_id' in item ? null : item.project_id;
    const docId = 'reference_id' in item ? null : item.document_id;
    if (!projectId || !docId) {
      setPendingDrop(null);
      return;
    }
    setImporting(true);
    try {
      const form = new FormData();
      form.append('file', drop.file);
      form.append('project_id', projectId);
      form.append('document_id', docId);
      const { markdown } = await apiClient.upload('/documents/extract-text', form) as { markdown: string };
      spliceAt(view, dropInsertSpec(view.state.doc, drop.pos, markdown, 'block'));
      setPendingDrop(null);
    } catch (err) {
      console.error('file-drop import failed', err);
      useAppStore.getState().showToast(t('failedToImportFile'), 'error');
      setPendingDrop(null);
    } finally {
      setImporting(false);
    }
  }, [t]);

  // Bound to a ref-read wrapper so the extension can live in the once-created
  // cmExtensions array while always invoking the latest handler.
  const fileDropExtension = useMemo(
    () => makeFileDropExtension((file, coords) => handleEditorFileDropRef.current(file, coords)),
    [],
  );

  return {
    fileDropExtension,
    modalProps: {
      pendingDrop,
      importing,
      onCancel: () => setPendingDrop(null),
      onConfirm: handleConfirmImport,
    },
  };
}
