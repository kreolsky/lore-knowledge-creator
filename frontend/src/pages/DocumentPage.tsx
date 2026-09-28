/** Document editor page — fetches document by URL param and sets it as currentDocument. Store: setCurrentDocument. */
import { useEffect, useCallback, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Editor } from '../components/Editor';
import { SplitEditorLayout } from '../components/editor/SplitEditorLayout';
import { TableFocusView } from '../components/editor/TableFocusView';
import { useAppStore } from '../store/app-store';
import { useDocState, useUIStore } from '../store/ui-store';
import { readRefOpenMode } from '../store/ui-store/documents-slice';
import { useNoteChatStore } from '../store/note-chat-store';
import { useNoteStore } from '../store/note-store';
import { apiClient, isAccessRefusal } from '../api/client';
import { loadReferences } from '../api/references-fetch';
import { resolveRestoredReference } from '../store/restore-entity';
import { clearLocalDocsForEntity } from '../collab/local-doc-persistence';
import { clearRecordingsForDocument } from '../utils/recording-cache';
import { useEvent } from '../hooks/useEvent';
import { useDocumentRoute } from '../hooks/useDocumentRoute';
import { isBareDocRoute } from '../utils/routing';
import { t } from '../i18n';
import type { ChatSession, Document, Project, Reference } from '../types';

export function DocumentPage() {
  const { projectId, documentId } = useDocumentRoute();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const setCurrentDocument = useAppStore(s => s.setCurrentDocument);
  // WHY: projectId/pathname are read through refs, NOT effect deps. Why: on the
  // bare /docs/<id> cold path the /open bundle itself sets currentProject, which flips
  // useDocumentRoute's projectId (undefined → id). As a dep that re-ran the open effect
  // mid-flight — its cleanup set cancelled=true, so the bundle was fetched and then
  // THROWN AWAY, and the concurrent fallback path re-fetched everything (the one-RTT
  // anti-flicker ARCH below silently never applied). Both values are stable for a given
  // documentId on every other path (route params change together), so nothing needs the
  // re-run.
  const projectIdRef = useRef(projectId);
  projectIdRef.current = projectId;
  const pathnameRef = useRef(pathname);
  pathnameRef.current = pathname;

  // ARCH: a URL-driven document open fetches the document AND its restored sub-state
  // (references list + note-chat sessions) CONCURRENTLY, then commits the document
  // together with its restored reference in a single render. Why: the previous shape
  // committed the document ref-less and patched the reference in as a second waterfall
  // stage — a visible flicker. See setCurrentDocument INVARIANT in app-store.
  useEffect(() => {
    if (!documentId) return;
    const projectId = projectIdRef.current;
    // Skip if this document is already the committed currentDocument — a remount
    // (StrictMode, shell re-render) must not re-fetch what is already on screen.
    // INVARIANT: this skip must self-reconcile note sessions before returning. Why:
    // the pre-commit nav path (Header.setCurrentDocument BEFORE navigate, fed by
    // tree / search / doc links / chat Sources) lands here with the whole open
    // bundle skipped — this branch is the ONLY note-session loader on that path, or
    // the Notes tab renders the previous scope (or nothing) under the open doc.
    // ensureSessions no-ops when the store's sessionsScope already matches (the
    // remount case stays zero-fetch) and otherwise runs the loadSessions reconcile.
    if (useAppStore.getState().currentDocument?.document_id === documentId) {
      if (projectId) void useNoteChatStore.getState().ensureSessions(projectId, documentId);
      return;
    }
    let cancelled = false;

    // ARCH(plan "public-document-ids"): bare /docs/:id cold path — the URL has no
    // projectId and currentProject is not yet hydrated. The one-shot /open bundle
    // seeds project + document + references (and triggers prefs + note-session
    // loads) in ONE round trip, preserving the concurrent-commit anti-flicker
    // invariant. The legacy nested route + in-app nav keep the concurrent per-id
    // path below (currentProject already in the store → projectId resolved).
    if (isBareDocRoute(pathnameRef.current) && !useAppStore.getState().currentProject) {
      (async () => {
        try {
          const bundle = await apiClient.get(`/documents/open/${documentId}`) as {
            document: Document;
            references?: Reference[];
            project?: Project | null;
            note_sessions?: ChatSession[];
          };
          if (cancelled) return;
          const refs: Reference[] = bundle.references ?? [];
          const project = bundle.project ?? null;
          if (project) {
            useAppStore.getState().setCurrentProject(project);
            // INVARIANT: the access level comes from the SAME payload as the project.
            // Why: the store default is 'full', and on this path the corrective
            // /projects/{id} fetch (useProjectConnection) lands one RTT later — a
            // readonly member would see edit affordances until then.
            if (project.my_access) useAppStore.getState().setAccessLevel(project.my_access);
          }
          const pid = project?.project_id;
          const userId = useAppStore.getState().currentUser?.user_id;
          // Hydrate per-project prefs (the persisted reference pointer) BEFORE
          // resolving the restored reference. Note sessions come from the bundle
          // itself — the point of the one-shot endpoint is that this path costs
          // ONE round trip; re-fetching /chat/sessions here would undo that.
          if (pid && userId) useUIStore.getState().loadProjectPrefs(userId, pid);
          if (pid) useNoteChatStore.getState().hydrateSessions(pid, documentId, bundle.note_sessions ?? []);
          await (pid ? useUIStore.getState().awaitProjectPrefs(pid) : Promise.resolve());
          if (cancelled) return;
          const restoredReference = resolveRestoredReference(documentId, refs);
          const cached = useAppStore.getState().documents.find(d => d.document_id === documentId);
          const docToCommit: Document = cached?.content !== undefined
            ? { ...bundle.document, content: cached.content, headings: cached.headings ?? bundle.document.headings }
            : bundle.document;
          setCurrentDocument(docToCommit, { references: refs, restoredReference });
        } catch (err) {
          console.error('Failed to open document:', err);
          // INVARIANT(security): a refusal here deletes this document's local mirror
          // and cached voice recordings; any other failure must not. Why: on the
          // bare-doc route no project is known yet and no collab socket is ever
          // opened, so this refusal is the only signal that the copies are no longer
          // ours to hold — while an outage is what they are FOR.
          // The refusal arrives as 404 (isAccessRefusal explains why it is not 403).
          if (isAccessRefusal(err)) {
            void clearLocalDocsForEntity(documentId);
            void clearRecordingsForDocument(documentId);
          }
          if (cancelled) return;
          // INVARIANT: a refusal hands this id to the public-share fallback instead
          // of bouncing. Why: the document may be PUBLISHED — a shared link opens for
          // everyone, and a session for another project must not make this visitor see
          // less than a logged-out one. PublicSharePage 404s on its own if there is no
          // live share, so nothing is disclosed. Non-refusal failures still say so and
          // leave (no silent degradation).
          if (isAccessRefusal(err)) {
            useUIStore.getState().markPublicFallback([documentId]);
            return;
          }
          useAppStore.getState().showToast(t('documentOpenFailed'), 'error');
          navigate('/');
        }
      })();
      return () => { cancelled = true; };
    }

    // Start the prefs load HERE so awaitProjectPrefs below has a real in-flight promise
    // to wait on. Why: on a cold open (projects-list / F5) loadProjectPrefs is otherwise
    // triggered only by setCurrentProject — i.e. after GET /projects resolves, later than
    // this mount — so the persisted reference pointer would not be hydrated in time and
    // the document would commit ref-less. Skip on same-project in-app nav (already loaded).
    const userId = useAppStore.getState().currentUser?.user_id;
    const ui = useUIStore.getState();
    const prefsAlreadyLoaded =
      ui.projectPrefsLoaded && useAppStore.getState().currentProject?.project_id === projectId;
    if (projectId && userId && !prefsAlreadyLoaded) {
      ui.loadProjectPrefs(userId, projectId);
    }

    (async () => {
      // track=1: this is the URL-driven intentional open — record it as last-accessed.
      // Auxiliary fetches (previews, Sources, snapshot banner) omit track so they don't
      // clobber the pointer. See documents.get_document INVARIANT.
      const docPromise = apiClient.get(`/documents/${documentId}?track=1`);
      // index_doc_id is redundant when document_id is passed — the server walks ancestors
      // and the project root is always an ancestor (references.get_references).
      const refsPromise: Promise<Reference[]> = projectId
        ? loadReferences(projectId, documentId).catch(() => [] as Reference[])
        : Promise.resolve([] as Reference[]);
      const sessionsPromise = projectId
        ? useNoteChatStore.getState().loadSessions(projectId, documentId)
        : Promise.resolve();
      // Await any in-flight project prefs so the persisted per-doc reference pointer is
      // hydrated before we read it (handles the F5 race that the deferred bridge covered).
      const prefsPromise = projectId
        ? useUIStore.getState().awaitProjectPrefs(projectId)
        : Promise.resolve();

      let data, refs;
      try {
        [data, refs] = await Promise.all([docPromise, refsPromise, sessionsPromise, prefsPromise]);
      } catch (err) {
        console.error('Failed to load document:', err);
        if (cancelled) return;
        // Same fallback as the cold path above: a refused id may be a published one.
        if (isAccessRefusal(err)) {
          useUIStore.getState().markPublicFallback([documentId]);
          return;
        }
        useAppStore.getState().showToast(t('documentOpenFailed'), 'error');
        navigate(projectId ? `/projects/${projectId}` : '/');
        return;
      }

      // Staleness guard: a fast project/doc switch must never commit stale state.
      // Bail only if a DIFFERENT project is already active — a still-null currentProject
      // is the normal deep-link/F5 race (project fetch not resolved yet) and must commit.
      if (cancelled) return;
      const activeProject = useAppStore.getState().currentProject;
      if (projectId && activeProject && activeProject.project_id !== projectId) return;

      const restoredReference = resolveRestoredReference(documentId, refs);

      const cached = useAppStore.getState().documents.find(d => d.document_id === documentId);
      // INVARIANT: client cache may be fresher than server during WS flush delay.
      // Prefer client content + headings; take updated_at from server.  Why: during the WS-flush delay the client cache can be fresher than the server; prefer client content+headings but take updated_at from the server (ordering source of truth).
      const docToCommit = cached?.content !== undefined
        ? { ...data, content: cached.content, headings: cached.headings ?? data.headings }
        : data;

      setCurrentDocument(docToCommit, { references: refs, restoredReference });
      // NOTE: setCurrentDocument owns the post-commit body hydrate of the WINNER
      // (finalRef = applyRef ?? restoredReference). Do NOT hydrate restoredReference
      // here — on a cross-doc ref-link click, applyRef (the pending target) wins and
      // hydrating restoredReference would yank currentReference off it (ref-link revert
      // regression, see app-store.ts commit() ARCH note).
    })();

    return () => { cancelled = true; };
  }, [documentId, setCurrentDocument, navigate]);

  const currentProject = useAppStore(s => s.currentProject);
  const currentDocument = useAppStore(s => s.currentDocument);

  useEvent('project-extraction-error', useCallback(({ noteId, documentId }: { noteId: string; documentId: string }) => {
    if (!currentProject || !currentDocument) return;
    if (documentId !== currentDocument.document_id) return;
    useNoteChatStore.getState().loadSessions(
      currentProject.project_id,
      currentDocument.document_id,
    ).then(() => {
      useNoteStore.getState().setActiveNoteThreadId(noteId, currentDocument.document_id);
      useNoteChatStore.getState().setActiveSession(noteId);
      useNoteChatStore.getState().setPendingInputFocus(true);
    });
  }, [currentProject?.project_id, currentDocument?.document_id]));

  // ARCH: an agent proposal apply failed and the backend pinned a system note to
  // this document. Refresh the notes panel + open the new error note thread so the
  // user sees the explanation (mirrors the extraction-error handler above).
  useEvent('project-agent-error-note', useCallback(({ noteId, documentId }: { noteId: string; documentId: string }) => {
    if (!currentProject || !currentDocument) return;
    if (documentId !== currentDocument.document_id) return;
    useNoteChatStore.getState().loadSessions(
      currentProject.project_id,
      currentDocument.document_id,
    ).then(() => {
      useNoteStore.getState().setActiveNoteThreadId(noteId, currentDocument.document_id);
      useNoteChatStore.getState().setActiveSession(noteId);
      useNoteChatStore.getState().setPendingInputFocus(true);
    });
  }, [currentProject?.project_id, currentDocument?.document_id]));

  const currentReference = useAppStore(s => s.currentReference);
  const currentTable = useAppStore(s => s.currentTable);
  const previewDocument = useAppStore(s => s.previewDocument);
  const snapshotPreview = useAppStore(s => s.snapshotPreview);
  const refOpenMode = readRefOpenMode(useDocState(currentDocument?.document_id ?? null));

  // WHY: split layout is shown only with a document AND a selected reference or
  // focused table, and never during snapshot/document-preview (those own the full center
  // via the single editor). Why: split-view spec — the right column appears only when a
  // reference OR a table is the focused secondary entity.
  // ARCH: a table focus keeps the document `<Editor>` mounted underneath (it owns the
  // collab handle the TableFocusView reads — see TableFocusView ARCH note). The focus
  // view is rendered as an opaque overlay that visually replaces the editor area, so the
  // handle stays published with zero collab rejoin on open/back.
  const splitActive = refOpenMode === 'split' && !!currentDocument
    && (!!currentReference || !!currentTable)
    && !previewDocument && !snapshotPreview;
  const tableInSplit = splitActive && !!currentTable;
  const tableFocusCentered = !!currentTable && !!currentDocument
    && !previewDocument && !snapshotPreview && !splitActive;

  if (splitActive) {
    return tableInSplit
      ? <SplitEditorLayout document={currentDocument} table={currentTable} />
      : <SplitEditorLayout document={currentDocument} reference={currentReference ?? undefined} />;
  }

  // 'panel' mode: the reference lives in the right panel's Refs tab (ReferencesPanel),
  // so the center is pinned to the document — otherwise Editor would swap it for the
  // reference. Tables are untouched: a focused table still centers as in 'center' mode.
  const panelPinsDocument = refOpenMode === 'panel' && !!currentDocument && !!currentReference
    && !previewDocument && !snapshotPreview;

  return (
    <div className="relative flex-1 flex overflow-hidden min-h-0 bg-bg">
      {panelPinsDocument ? <Editor entity={currentDocument} role="primary" /> : <Editor />}
      {tableFocusCentered && (
        <div className="absolute inset-0 z-20 flex flex-col overflow-hidden bg-bg">
          <TableFocusView key={currentTable.table_id} table={currentTable} />
        </div>
      )}
    </div>
  );
}
