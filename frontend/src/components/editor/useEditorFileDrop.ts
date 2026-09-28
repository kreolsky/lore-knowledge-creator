/**
 * File-drop import for the document editor (drag a .docx/text file into the body).
 *
 * Owns the pending-drop/importing state, the drop type-guard, and the
 * confirm→upload→insert flow. Editor.tsx keeps computing the editable gate
 * (it lives with the collab state) and passes it in via a ref-read getter.
 */
// SYSTEM: editor — file-drop import (drag a .docx/text file into the body).

import { useState, useRef, useCallback, useMemo } from 'react';
import type React from 'react';
import type { EditorView } from '@codemirror/view';
import { makeFileDropExtension } from '../../editor/file-drop-handler';
import { useTranslation } from '../../i18n';
import { useAppStore } from '../../store/app-store';
import { apiClient } from '../../api/client';
import type { Document, Reference } from '../../types';
import { getEntityHandle } from '../../collab/active-handle-registry';
import { getActiveHandle, getViewEntity } from '../../editor/active-editor';
import { createTable, tableAnchor, normalizeTableLabel } from '../editor/live-preview/table-block-model';
import { sniffDelimiter, parseCsv, MAX_TABLE_IMPORT_ROWS, MAX_TABLE_IMPORT_COLS, MAX_TABLE_IMPORT_CELLS } from '../editor/live-preview/csv-table';
import { isTableFile, MAX_MARKDOWN_SIZE_MB } from '../references/ref-utils';

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
  // round-trip); creates the table model + splices the `![label](table:id)` anchor at the
  // drop line end (mirrors paste's importGfmTables). System: table-block.
  const handleTableInsert = useCallback(async (file: File, pos: number) => {
    const view = editorViewRef.current;
    // ARCH (split view): the table lands in the DROP VIEW'S entity ydoc — the focused
    // slot may hold the other column's handle. Unknown views keep the slot fallback
    // (parity with paste-handler's insertVerbatim targeting).
    // WHY: a KNOWN entity whose handle has not landed must fail loudly here (toast),
    // never import into the slot's foreign ydoc — model and anchor would land in
    // different ydocs.
    const entityId = view ? getViewEntity(view) : null;
    const ydoc = entityId ? getEntityHandle(entityId)?.ydoc : getActiveHandle()?.ydoc;
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
      const line = view.state.doc.lineAt(pos);
      const anchor = tableAnchor(label, id);
      const insertText = `\n${anchor}`;
      view.dispatch({
        changes: { from: line.to, insert: insertText },
        selection: { anchor: line.to + insertText.length },
      });
      view.focus();
    } catch (err) {
      console.error('CSV/TSV table drop failed', err);
      useAppStore.getState().showToast(t('csvImportFailed'), 'error');
    }
  }, [t]);
  const handleTableInsertRef = useRef(handleTableInsert);
  handleTableInsertRef.current = handleTableInsert;

  // INVARIANT: client-side type guard is permissive — final validation is  Why: the client guard is permissive (rejects only obvious binary) to avoid a useless round-trip; final validation is server-side via is_text_bytes.
  // server-side via is_text_bytes. We only reject obvious binary (not docx and
  // not text-ish MIME) to avoid a useless upload round-trip. CSV/TSV is handled
  // by a dedicated local-parse path (no modal).
  const handleEditorFileDrop = useCallback((file: File, pos: number) => {
    const { isReadonly: ro, editable: ed } = getDropGuard();
    if (ro || !ed) return; // readonly/offline viewers don't insert
    const name = file.name.toLowerCase();
    if (isTableFile(name)) {
      void handleTableInsertRef.current(file, pos); // fire-and-forget; toasts handle errors
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
    setPendingDrop({ file, pos });
  }, [t, getDropGuard]);
  const handleEditorFileDropRef = useRef(handleEditorFileDrop);
  handleEditorFileDropRef.current = handleEditorFileDrop;

  // Confirm → upload → insert below drop position.
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
      const line = view.state.doc.lineAt(drop.pos);
      const insertText = `\n${markdown}`;
      view.dispatch({
        changes: { from: line.to, insert: insertText },
        selection: { anchor: line.to + insertText.length },
      });
      view.focus();
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
    () => makeFileDropExtension((file, pos) => handleEditorFileDropRef.current(file, pos)),
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
