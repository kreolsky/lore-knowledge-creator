/** Resolve { projectId, documentId } for BOTH URL shapes (plan "public-document-ids").
 *
 *   - /projects/:projectId/docs/:documentId → both ids from route params.
 *   - /docs/:documentId                      → documentId from the route param,
 *     projectId from the store (currentProject.project_id), populated on the
 *     bare-URL cold path by the /open bundle (see DocumentPage).
 *
 * ARCH: the bare /docs/<id> route carries NO projectId. Several shell-wide
 * consumers (useProjectConnection, Sidebar, useCollabConnection, DocumentPage)
 * previously read projectId via useParams() and broke on that route (undefined →
 * WS never opened, /projects/:id fetch 401'd, navigate fallbacks dead-ended).
 * Centralizing the read here makes the store fallback the single seam: once the
 * /open bootstrap sets currentProject, every shell consumer re-resolves in sync.
 *
 * INVARIANT: route projectId wins over the store. Why: the legacy nested route
 * (/projects/:projectId/…) is the authoritative source when present; the store is
 * only the fallback for the param-less bare route. */
// SYSTEM: use-document-route — {projectId, documentId} for both URL shapes

import { useParams } from 'react-router-dom';
import { useAppStore } from '../store/app-store';

export function useDocumentRoute(): { projectId: string | undefined; documentId: string | undefined } {
  const { projectId: routeProjectId, documentId } = useParams();
  const storeProjectId = useAppStore(s => s.currentProject?.project_id);
  return { projectId: routeProjectId ?? storeProjectId, documentId };
}
