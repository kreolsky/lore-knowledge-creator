/**
 * Shared restore helper for the URL-driven document open.
 *
 * ARCH: both the DocumentPage parallel-open path and the setCurrentDocument
 * restoreFlow fallback resolve the persisted per-doc reference pointer against a
 * freshly-fetched references list the same way. Centralised here so the find +
 * stale-pointer cleanup cannot drift between the two call sites.
 */
import { useUIStore } from './ui-store';
import { refIsScope } from './ui-store/documents-slice';
import type { Reference } from '../types';

/**
 * Id-only restore stub: the saved pointer of a 'panel' quick preview whose reference
 * is not in the document's list (cross-doc). It carries NOTHING but the id — no
 * title, no media_type — so it must never be rendered or committed as
 * `currentReference`; setCurrentDocument's commit() hands the id to the id-only
 * hydrate instead (see isRestoreStub).
 */
export function isRestoreStub(ref: Reference): boolean {
  return !('title' in ref);
}

/**
 * Resolve the reference to restore for a document from a fetched references list.
 *
 * Reads the persisted per-doc pointer (`getCurrentReferenceForDoc`); if it points
 * at a reference no longer present in `refs`, the pointer is stale — EXCEPT in
 * 'panel' quick preview, where the saved id is kept as an id-only stub
 * (`{ reference_id }`): a cross-doc reference previewed in this document is not
 * in the doc's ancestor-scoped list, and setCurrentDocument's post-commit hydrate
 * fills the body from the id. Other modes clear the stale pointer and return null.
 */
export function resolveRestoredReference(
  documentId: string,
  refs: Reference[],
): Reference | null {
  const ui = useUIStore.getState();
  const savedRefId = ui.getCurrentReferenceForDoc(documentId);
  if (!savedRefId) return null;
  const restored = refs.find(r => r.reference_id === savedRefId) ?? null;
  if (restored) return restored;
  if (!refIsScope(ui.getRefOpenMode(documentId))) {
    // Quick preview: keep the saved id as an id-only stub (isRestoreStub) — commit()
    // reads only reference_id from it and runs the id-only hydrate, which promotes
    // the full reference on resolve.
    return { reference_id: savedRefId } as Reference;
  }
  ui.setCurrentReferenceForDoc(documentId, null);
  return null;
}
