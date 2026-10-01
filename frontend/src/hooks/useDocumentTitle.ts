/** A document's title from the loaded project tree — the preview popup's plaque. */
// WHY a hook of its own, not a field of useDocumentPreview: that module is imported by
// editor-host, which app-store imports, so reading the store there would close an import
// cycle. A document outside the loaded tree has no title here (undefined → no plaque).

import { useAppStore } from '../store/app-store';

export function useDocumentTitle(docId: string | null): string | undefined {
  return useAppStore(s => docId ? s.documents.find(d => d.document_id === docId)?.title : undefined);
}
