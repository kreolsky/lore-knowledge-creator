/** Open a document id: commit-then-navigate — the ONE owner of document navigation.
 *
 * SYSTEM: document-navigation — commit-then-navigate for a document id; every
 * writer goes through here (the bus handler via useDocumentNavigation; direct
 * callers like useEditorEvents' cross-doc ref branch).
 *
 * Default = open the DOCUMENT BODY: focusDocument runs first and clears the
 * target's remembered per-doc reference pointer (or unfocuses in place when the
 * doc is already open). `restore: true` = reopen the document AS IT WAS — its
 * remembered last-opened reference; passed only by the tree, the one restoring
 * surface, and it skips focusDocument so the pointer survives.
 *
 * INVARIANT: the target document is committed (setCurrentDocument) BEFORE
 * navigate(), in that order, for EVERY writer. Why: DocumentPage's URL-driven
 * prefetch commit is DROPPED when the store shows a different doc
 * (INVARIANT(doc-desync) in app-store.setCurrentDocument), so a bare navigate
 * changes the URL and leaves the PREVIOUS document on screen.
 */

import type { NavigateFunction } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { docUrl } from '../utils/routing';

export function openDocument(
  docId: string,
  { restore, navigate }: { restore?: true; navigate: NavigateFunction },
): void {
  const state = useAppStore.getState();
  const doc = state.documents.find(d => d.document_id === docId);
  if (!doc) return;
  // Default path: focusDocument has already unfocused a same-doc reference/table —
  // 'stayed' means the URL is already the doc's, so return without re-committing or
  // re-navigating (no blink). Every other case (incl. same-doc-nothing-focused,
  // which returns 'navigate') runs the normal path.
  if (!restore && state.focusDocument(docId) === 'stayed') return;
  state.setCurrentDocument(doc);
  if (state.currentProject) {
    // Canonical /docs/<id> URL (plan "public-document-ids") — one shape for
    // every navigate-to-document source (tree, search, chat Sources, links,
    // editor clicks) since they all go through here.
    navigate(docUrl(docId));
  }
}
