/** Single writer for editor content → Zustand store + CRDT flush.
 *
 * INVARIANT: syncToStore uses setState directly. Never setCurrentDocument /
 * setCurrentReference — both reset snapshotPreview.  Why: setCurrentDocument/setCurrentReference reset snapshotPreview as a side effect; syncToStore writes via setState directly so it doesn't clobber the preview.
 */
// SYSTEM: content-sync — single writer for editor content to store + persist

import type React from 'react';
import type { EditorView } from '@codemirror/view';
import type { WsStatus } from '../collab/ws-status';
import type { YjsProjectProvider } from '../collab/yjs-provider';
import type { Document, Reference } from '../types';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';
import { invalidatePreviewCache } from '../hooks/useDocumentPreview';
import { invalidateLinkCache } from '../api/links';

export type Item = Document | Reference;

export interface PersistOptions {
  collabStatus: WsStatus | undefined;
  projectCollab: YjsProjectProvider | null;
}

const lastCheckpointContent = new Map<string, string>();

export function clearCheckpointDedup(entityId: string) {
  lastCheckpointContent.delete(`d:${entityId}`);
  lastCheckpointContent.delete(`r:${entityId}`);
}

export function clearAllCheckpointDedup() {
  lastCheckpointContent.clear();
}

/** Read content from CM6 view. Returns null if view is gone. */
export function readEditorContent(
  editorViewRef: React.RefObject<EditorView | null>,
): string | null {
  return editorViewRef.current?.state.doc.toString() ?? null;
}

/** Merge new content into a Document, matching by document_id. Pure function. */
export function mergeContentIntoDoc(d: Document, id: string, content: string): Document {
  return d.document_id === id ? { ...d, content } : d;
}

export function syncToStore(item: Item | null, content: string): void {
  if (!item) return;
  const store = useAppStore.getState();

  if ('reference_id' in item) {
    store.updateReference(item.reference_id, { content });
    invalidateLinkCache('ref', item.reference_id);
    if (store.currentReference?.reference_id === item.reference_id) {
      useAppStore.setState({
        currentReference: { ...store.currentReference, content },
      });
    }
    return;
  }

  store.setDocuments(
    store.documents.map(d => mergeContentIntoDoc(d, item.document_id, content)),
  );
  invalidatePreviewCache(item.document_id);
  invalidateLinkCache('doc', item.document_id);
  if (store.currentDocument?.document_id === item.document_id) {
    useAppStore.setState({
      currentDocument: { ...store.currentDocument, content },
    });
  }
}

/** Flush pending ydoc updates for the entity over the collab socket — the
 * editor's ONLY write door for text (the CRDT). A failure toasts (no silent
 * degradation) and rethrows so the checkpoint dedup cache does not record it.
 * Not connected → no-op: the view is non-editable unless collab is connected
 * (Editor.tsx INVARIANT), and the IndexedDB mirror holds unflushed edits until
 * resync. */
export async function persist(item: Item, options: PersistOptions): Promise<void> {
  if (options.collabStatus !== 'connected' || !options.projectCollab) return;
  const entityId = 'reference_id' in item ? item.reference_id : item.document_id;
  try {
    await options.projectCollab.flushAndWait(entityId);
  } catch (err) {
    console.warn('persist: WS flushAndWait failed', err);
    useAppStore.getState().showToast(t('failedToSaveDocument'), 'error');
    throw err;
  }
}

/** Single checkpoint: read CM6 → write store → persist. Returns the persist promise.
 *
 * Per-document content dedup: skips persist if content hasn't changed since the
 * last checkpoint for this entity. Prevents double WS flush under React strict mode.
 */
export function checkpointContent(
  editorViewRef: React.RefObject<EditorView | null>,
  item: Item | null,
  options: PersistOptions,
): Promise<void> {
  const content = readEditorContent(editorViewRef);
  if (content === null || !item) return Promise.resolve();
  // INVARIANT(data-loss): never persist an empty/whitespace-only editor read.
  // Why: a view born before its yCollab binding lands reads "" while the server
  // still holds the real text — checkpointing it on an entity switch before
  // sync is the empty-view wipe (plan .kilo/plans/empty-checkpoint-wipe.md).
  // A legitimate empty state cannot be produced from this UI: editing requires
  // the live-collab phase, and there every keystroke already streams via the
  // ydoc path, so the server holds the empty state before any checkpoint. The
  // never-bound view born empty is the ONLY producer of an empty read.
  // Scope: this guard covers every read the editor's checkpoint makes — the
  // flushAndWait door below writes ydoc updates, never the raw read. A REST
  // content write now only comes from no-session API clients, where the backend
  // empty-wipe guard (backend/documents/update.py::_reject_empty_wipe, 409)
  // applies.
  if (!content.trim()) {
    console.warn(
      '[content-sync] checkpoint skipped: empty editor read (never-bound view?)',
      'reference_id' in item ? item.reference_id : item.document_id,
    );
    return Promise.resolve();
  }
  const key = 'reference_id' in item ? `r:${item.reference_id}` : `d:${item.document_id}`;
  syncToStore(item, content);
  if (lastCheckpointContent.get(key) === content) return Promise.resolve();
  return persist(item, options).then(() => {
    lastCheckpointContent.set(key, content);
  });
}
