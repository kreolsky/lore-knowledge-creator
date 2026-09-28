/** Project-level WS + collab WS connection lifecycle. */
// ARCH: ProjectConnection handles tree changes and lifecycle notifications.
// ProjectCollabConnection is a persistent collab WS shared via context.

import { useEffect, useRef, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../store/app-store';
import { apiClient, isAccessRefusal } from '../api/client';
import { emit } from '../events';
import { useEvent } from './useEvent';
import { ProjectConnection } from '../collab/project-connection';
import { YjsProjectProvider } from '../collab/yjs-provider';
import { clearProjectLocalDocs } from '../collab/local-doc-persistence';
import { clearProjectRecordings } from '../utils/recording-cache';
import { useChatStore } from '../store/chat-store';
import { dispatchChatFrame } from '../store/chat-store/streaming';
import { t } from '../i18n';

interface UseProjectConnectionParams {
  projectId: string | undefined;
}

export function useProjectConnection({ projectId }: UseProjectConnectionParams) {
  const navigate = useNavigate();
  const setCurrentProject = useAppStore(s => s.setCurrentProject);
  const setAccessLevel = useAppStore(s => s.setAccessLevel);
  const projectConnRef = useRef<ProjectConnection | null>(null);
  const [projectCollabConn, setProjectCollabConn] = useState<YjsProjectProvider | null>(null);

  // INVARIANT: `navigate` is read through a ref, never listed as an effect dep.
  // Why: react-router's useNavigate rebuilds its callback whenever the pathname
  // changes (locationPathname sits in its useCallback deps), so depending on it
  // re-ran this effect on EVERY in-app navigation — a redundant GET /projects/:id
  // per document switch, plus a fresh `currentProject` identity that re-rendered
  // every subscriber. The project fetch belongs to the project, not to the URL.
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;

  useEffect(() => {
    if (!projectId) return;
    // INVARIANT: a settlement arriving after this effect is torn down touches
    // neither the store nor the router.
    // Why: switching projects (or leaving) leaves the previous fetch in flight;
    // without the guard its rejection toasts an error about a project the user
    // already left and yanks them to '/' mid-navigation.
    let cancelled = false;
    apiClient.get(`/projects/${projectId}`).then((data) => {
      if (cancelled) return;
      setCurrentProject(data.project);
      setAccessLevel(data.project.my_access || 'readonly');
    }).catch((err) => {
      console.error('Failed to load project:', err);
      // INVARIANT(security): a refusal on the project deletes its local mirrors AND
      // cached voice recordings; any other failure must NOT. Why: this is the only
      // place a revoked user learns the verdict — they never open a document, so no
      // collab socket and no join ever happens, and a live drive found the text still
      // readable straight out of IndexedDB. An outage (5xx, network error) is the case
      // the mirrors and the recording cache EXIST for, so it deletes nothing.
      // The refusal arrives as 404 (isAccessRefusal explains why it is not 403).
      if (isAccessRefusal(err)) {
        void clearProjectLocalDocs(projectId);
        void clearProjectRecordings(projectId);
      }
      if (cancelled) return;
      // No silent degradation: a bare bounce to '/' reads as "broken link" for
      // what is usually a permissions or outage failure. Say it, then leave —
      // the toast-then-redirect twin of DocumentPage's documentOpenFailed.
      useAppStore.getState().showToast(t('projectLoadFailed'), 'error');
      navigateRef.current('/');
    });
    return () => { cancelled = true; };
  }, [projectId, setCurrentProject, setAccessLevel]);

  useEffect(() => {
    if (!projectId) return;
    const conn = new ProjectConnection(projectId, {
      onDocumentCreated: (documentId, title, parentId, sortKey) =>
        emit('project-document-created', { documentId, title, parentId, sortKey }),
      onDocumentRenamed: (documentId, title) =>
        emit('project-document-renamed', { documentId, title }),
      onDocumentMoved: (documentId, parentId, sortKey, previousParentId, isReference, title) =>
        emit('project-document-moved', { documentId, parentId, sortKey, previousParentId, isReference, title }),
      onDocumentReordered: (documentId, parentId, sortKey) =>
        emit('project-document-reordered', { documentId, parentId, sortKey }),
      onDocumentDeleted: (documentId) =>
        emit('project-document-deleted', { documentId }),
      onDocumentsDeletedBatch: (documentIds, referenceIds) =>
        emit('project-documents-deleted-batch', { documentIds, referenceIds }),
      onDocumentsMovedOut: (documentIds, referenceIds, targetProjectId, targetProjectName) =>
        emit('project-documents-moved-out', { documentIds, referenceIds, targetProjectId, targetProjectName }),
      onDocumentsMovedIn: (documentIds) =>
        emit('project-documents-moved-in', { documentIds }),
      onReferenceCreated: (referenceId, title, documentId, createdBy, createdByName) =>
        emit('project-reference-created', { referenceId, title, documentId, createdBy, createdByName }),
      onReferenceRenamed: (referenceId, title) =>
        emit('project-reference-renamed', { referenceId, title }),
      onReferenceMoved: (referenceId, documentId) =>
        emit('project-reference-moved', { referenceId, documentId }),
      onReferenceDeleted: (referenceId) =>
        emit('project-reference-deleted', { referenceId }),
      onReferenceUpdated: (referenceId) =>
        emit('project-reference-updated', { referenceId }),
      onReferenceStatusChanged: (referenceId, status) =>
        emit('project-reference-status-changed', { referenceId, status }),
      onContentFlushed: (entityId, entityType) =>
        emit('project-content-flushed', { entityId, entityType }),
      onAgentExtractionStarted: (referenceId) =>
        emit('project-agent-extraction-started', { referenceId }),
      onGenerateImageProgress: (sessionId, phase, runId, messageId) =>
        emit('project-generate-image-progress', { sessionId, phase, runId, messageId }),
      onGenerateImageDone: (e) =>
        emit('project-generate-image-done', e),
      onGenerateImageFailed: (e) =>
        emit('project-generate-image-failed', e),
      onExtractionError: (referenceId, noteId, documentId) => {
        // INVARIANT: extraction-failure toast is persistent. Why: the single-slot 3s
        // auto-dismiss toast was raced/clobbered by the "extraction started" info toast
        // and clearToast() on collab 'connected', so the error vanished while only the
        // (easy-to-miss) error note remained. A pipeline failure must stay visible until
        // the user dismisses it.
        useAppStore.getState().showToast(t('pipelineExtractionFailed'), 'error', { persistent: true });
        emit('project-extraction-error', { referenceId, noteId, documentId });
      },
      onAgentErrorNote: (noteId, documentId) => {
        // ARCH: an agent proposal apply failed and the backend recorded a system note
        // on the document. Surface a persistent toast (mirrors extraction_error) and
        // forward the note id so DocumentPage refreshes the notes panel + opens it.
        useAppStore.getState().showToast(t('agentApplyFailed'), 'error', { persistent: true });
        emit('project-agent-error-note', { noteId, documentId });
      },
      // Plan agent-line-harness-lifecycle step 7: the harness lifecycle's chat
      // frames enter the chat store's ONE sink here — the connection stays
      // transport-dumb, the dispatch decides (registered turn → the same
      // reducer the frame dispatch runs; no open turn → ignored).
      onChatFrame: (sessionId, frame) => {
        dispatchChatFrame(useChatStore.getState, useChatStore.setState, sessionId, frame);
      },
      // Plan agent-line-harness-lifecycle step 8: chat frames lost in a
      // project-WS outage recover through the reload + re-adoption.
      onProjectWsResync: () => {
        void useChatStore.getState().resyncOpenHarnessTurn();
      },
      onEmbeddingDegraded: () => {
        useAppStore.getState().showToast(t('embeddingsIndexFailed'), 'error');
      },
      onEmbeddingRecovered: () => {
        useAppStore.getState().showToast(t('embeddingsIndexRecovered'), 'info');
      },
      onProjectUpdated: (updates) =>
        emit('project-updated', updates as Record<string, unknown>),
      onProjectDeleted: () =>
        emit('project-deleted'),
      onStatusChange: () => {},
      onError: (msg) => useAppStore.getState().showToast(msg, 'error'),
    });
    conn.connect();
    projectConnRef.current = conn;
    return () => {
      conn.disconnect();
      projectConnRef.current = null;
    };
  }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    const userId = useAppStore.getState().currentUser?.user_id ?? '';
    const conn = new YjsProjectProvider(projectId, userId);
    conn.connect();
    setProjectCollabConn(conn);
    return () => {
      conn.disconnect();
      setProjectCollabConn(null);
    };
  }, [projectId]);

  useEvent('project-deleted', useCallback(() => navigate('/'), [navigate]));

  return { projectCollabConn };
}
