/** Editor utility functions — reference list re-fetch into the store. */

import { useAppStore } from '../store/app-store';
import { loadReferences } from '../api/references-fetch';
import { t } from '../i18n';

/** Re-fetch references for current document/project into Zustand store. */
export async function fetchReferences(): Promise<void> {
  const { currentProject, currentDocument } = useAppStore.getState();
  if (!currentProject || !currentDocument) return;
  try {
    // INVARIANT: share the in-flight dedup with ReferencesPanel + setCurrentDocument's
    // restore fetch — all three fire for the same (project, doc) on a doc switch;
    // collapsing to ONE round-trip keeps the switch a single panel load. Why:
    // api/references-fetch.ts. index_doc_id is no
    // longer passed: the server resolves the project index doc itself.
    const refs = await loadReferences(
      currentProject.project_id, currentDocument.document_id,
    );
    useAppStore.getState().mergeReferences(refs);
  } catch (err) {
    console.error('Failed to load references', err);
    useAppStore.getState().showToast(t('failedToLoadReferences'), 'error');
  }
}
