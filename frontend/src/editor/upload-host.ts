/** The document that hosts a file uploaded from an editor view — shared by image drop and cross-project paste. */

import type { EditorView } from '@codemirror/view';
import { useAppStore } from '../store/app-store';
import type { Document, Reference } from '../types';
import { getViewEntity } from './active-editor';

/**
 * The host document of an upload made from `view`, or null when there is none.
 * WHY by the view's entity, never the focused slot: in split view the slot may hold the
 * other column (a table cell is registered to its host's entity at mount). A view on an
 * open REFERENCE uploads under that reference's host document — a reference cannot host
 * references; an unhosted reference yields null. An unregistered view uses `fallback`.
 */
export function viewHostDocument(view: EditorView, fallback: Document | Reference | null): Document | null {
  const store = useAppStore.getState();
  const entityId = getViewEntity(view);
  const item: Document | Reference | null = entityId
    ? store.documents.find(d => d.document_id === entityId)
      ?? store.references.find(r => r.reference_id === entityId)
      ?? null
    : fallback;
  return item && 'reference_id' in item
    ? store.documents.find(d => d.document_id === item.document_id) ?? null
    : item;
}
