/**
 * Table-badge operations for the ReferencesPanel — delete / insert-fallback /
 * download / rename for the document's native tables.
 *
 * Extracted from ReferencesPanel so the delete path is unit-testable without
 * mounting the panel (see useTableBadgeOps.test.ts). The handlers are
 * identity-scoped: they resolve the DOCUMENT's entity handle
 * (`getEntityHandle(currentDocument.document_id)`), never the focused slot —
 * with a reference column focused the slot holds the reference's handle, and a
 * slot-read op would mutate/export the WRONG ydoc (or silently no-op).
 */

import { useCallback } from 'react';
import type { EditorView } from '@codemirror/view';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { getEntityHandle } from '../collab/active-handle-registry';
import {
  deleteTableWithBackup, renameTable, normalizeTableLabel, tableAnchor,
  insertTableAnchor, readTableModel, type DocumentTableEntry,
} from '../components/editor/live-preview/table-block-model';
import { serializeTableCsv } from '../components/editor/live-preview/csv-table';
import type { Document } from '../types';
import type { CurrentTable } from '../store/app-store';

export interface TableBadgeOpsParams {
  currentDocument: Document | null;
  currentTable: CurrentTable | null;
  setCurrentTable: (t: CurrentTable | null) => void;
  /** Live focused-view getter (useEditorView) — the insert-at-cursor source. */
  getView: () => EditorView | null;
  tableRenameValue: string;
  setTableRenamingId: (id: string | null) => void;
}

export function useTableBadgeOps({
  currentDocument,
  currentTable,
  setCurrentTable,
  getView,
  tableRenameValue,
  setTableRenamingId,
}: TableBadgeOpsParams) {
  const { t } = useTranslation();

  const handleTableDelete = useCallback(async (entry: DocumentTableEntry) => {
    if (!currentDocument) return;
    // Guard the handler, not just the UI: a table delete is a full-access-only mutation.
    if (useAppStore.getState().accessLevel !== 'full') return;
    // Identity-scoped: the DOCUMENT's entity handle, not the focused slot (which may
    // hold the open reference's handle in split view). Null → explicit failure, no
    // silent return (No silent degradation).
    const handle = getEntityHandle(currentDocument.document_id);
    if (!handle?.ydoc) {
      useAppStore.getState().showToast(t('tableDeleteFailed'), 'error');
      return;
    }
    const documentId = currentDocument.document_id;
    try {
      // Force a checkpoint (content: null → server captures the LIVE ydoc, table still
      // present) BEFORE dropping the table, so the delete is always recoverable via the
      // History panel. On checkpoint failure the delete is aborted (No silent degradation).
      await deleteTableWithBackup(handle.ydoc, entry.table_id, entry.label, async () => {
        await apiClient.post('/checkpoints', {
          document_id: documentId,
          content: null,
          label: t('tableDeleteBackupLabel'),
        });
      });
    } catch (err) {
      console.error('Failed to back up before table delete', err);
      useAppStore.getState().showToast(t('tableDeleteFailed'), 'error');
      return;
    }
    if (currentTable?.table_id === entry.table_id) setCurrentTable(null);
    getView()?.focus();
  }, [currentDocument, currentTable, setCurrentTable, getView, t]);

  const handleTableInsert = useCallback((entry: DocumentTableEntry) => {
    if (!currentDocument) return;
    // Re-inserting the transclusion mutates content — full-access only (guard the handler).
    if (useAppStore.getState().accessLevel !== 'full') return;
    const anchor = tableAnchor(normalizeTableLabel(entry.label), entry.table_id);
    const view = getView();
    if (view) {
      // Insert at the cursor so the user places the re-linked table where they are reading.
      const pos = view.state.selection.main.head;
      view.dispatch({ changes: { from: pos, insert: anchor }, selection: { anchor: pos + anchor.length } });
      view.focus();
      return;
    }
    // No live view (mount race) → append to the DOCUMENT's ydoc content end. A null
    // entity handle fails loudly (No silent degradation) — the slot is not a fallback.
    const handle = getEntityHandle(currentDocument.document_id);
    if (!handle?.ydoc) {
      useAppStore.getState().showToast(t('tableInsertFailed'), 'error');
      return;
    }
    insertTableAnchor(handle.ydoc, entry.table_id, entry.label);
  }, [currentDocument, getView, t]);

  // Export a native table back to CSV (UTF-8 with BOM — Excel opens Cyrillic correctly).
  // Read-only: NOT gated by access level (a viewer/export can download without edit rights).
  const handleTableDownload = useCallback((entry: DocumentTableEntry) => {
    const handle = currentDocument ? getEntityHandle(currentDocument.document_id) : null;
    if (!handle?.ydoc) {
      useAppStore.getState().showToast(t('csvExportFailed'), 'error');
      return;
    }
    const model = readTableModel(handle.ydoc, entry.table_id);
    if (!model) {
      useAppStore.getState().showToast(t('csvExportFailed'), 'error');
      return;
    }
    const csv = serializeTableCsv(model);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const stem = normalizeTableLabel(entry.label) || 'table';
    a.download = `${stem}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [currentDocument, t]);

  const handleTableRename = useCallback((entry: DocumentTableEntry) => {
    // Normalize first so `]`/newline can never produce a broken anchor (see
    // normalizeTableLabel). An empty/unchanged result cancels the rename (reject, not
    // allow — an empty alt-text is a degraded anchor).
    const normalized = normalizeTableLabel(tableRenameValue);
    if (!normalized || normalized === entry.label) {
      setTableRenamingId(null);
      return;
    }
    const handle = currentDocument ? getEntityHandle(currentDocument.document_id) : null;
    // Null handle → explicit failure (No silent degradation): a silent skip would look
    // like an accepted rename that never lands.
    if (!handle?.ydoc) {
      useAppStore.getState().showToast(t('tableRenameFailed'), 'error');
      setTableRenamingId(null);
      return;
    }
    renameTable(handle.ydoc, entry.table_id, entry.label, normalized);
    setTableRenamingId(null);
    // No manual currentTable/currentTableLabel update — the reactive label pipeline in
    // the panel picks up the new label from the `tables` list once the content Y.Text
    // changes.
  }, [tableRenameValue, currentDocument, setTableRenamingId, t]);

  return { handleTableDelete, handleTableInsert, handleTableDownload, handleTableRename };
}
