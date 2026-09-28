/** Mount the ONE navigate-to-document bus handler (SYSTEM: document-navigation).
 *
 * Mounted once by ProjectShell — the Shell renders on BOTH the authed project
 * page and the public-share page, exactly the two surfaces the Header's handler
 * used to cover (Layout's non-editor Header sees no navigate-to-document
 * emitter: tree, chat, editor and search all render inside the editor shell).
 */

import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useUIStore } from '../store/ui-store';
import { useEvent } from '../hooks/useEvent';
import { openDocument } from './open-document';

export function useDocumentNavigation(): void {
  const navigate = useNavigate();
  const isPublicShare = useUIStore(s => s.isPublicShare);
  // PUBLIC: skip — PublicSharePage mounts its own navigate-to-document handler.
  // Both handlers fire on the bus; this one must early-return so the public
  // surface's in-place hydrate is the only one that runs (and because the store
  // lookups in openDocument assume the authed documents/currentProject state).
  useEvent('navigate-to-document', useCallback(({ documentId, restore }) => {
    if (isPublicShare) return;
    openDocument(documentId, { restore, navigate });
  }, [navigate, isPublicShare]));
}
