/**
 * PublicSharePage — anonymous, read-only view of a published doc or subtree.
 *
 * SYSTEM: public-share (FE entry) — renders /docs/<id> (anonymous branch of the
 * SessionRoute decider) with no AuthGuard. Same chrome as the authenticated
 * project surface via <ProjectShell>: same Header (trimmed), same left tab bar
 * (toc only for doc-scope; docs+toc for subtree), same Editor render path
 * (CM6 + render-bundle — see <PublicEditor>), same ReferencesPanel (readonly).
 * Notes tab (<PublicNotesPanel>) only while the covering share has comments on.
 * Live notes/text over ONE anonymous change-ping socket per viewed document
 * (usePublicDocSocket): the page holds no timer, and the refetch it runs on a
 * ping is the same the old 5 s poll ran (plus after the guest's own write).
 * No collab: the guest never edits the text.
 *
 * ARCH (plan "public-document-ids"): the page is now keyed on the document's own
 * uuid (a `documentId` prop), NOT a share token. The funnel is re-keyed:
 * publicTreeByDoc/publicDocumentByDoc/publicReferencesByDoc hit the canonical
 * /api/public/documents/{id}/* surface; the legacy /s/:token route resolves the
 * token to its root document_id and <Navigate replace>s to /docs/<id> (see
 * App.tsx). Doc selection within a subtree now CHANGES THE ROUTE
 * (navigate(/docs/<childId>)) — one id per document — replacing the prior
 * stay-on-/s/:token behavior.
 *
 * HYDRATION: on mount, sets `isPublicShare=true` + `accessLevel='readonly'` so
 * shared panels route fetches/files through the anonymous surface. The public
 * file context is seeded with the subtree ROOT document id (from publicTree) so
 * referenceFileUrl/referenceThumbUrl build /api/public/documents/<root>/files/…
 * (resolve_share walks ancestors, so one stable root covers every file). Reads
 * publicTree + the document bundle and commits them to the existing store slices
 * (documents, currentDocument, references).
 *
 * INVARIANT(security): this page is read-only unless the covering share has
 * `allow_comments`, and then its only write is a note — through the public notes
 * routes, which check access.can_write_note. Why: "доступ для всех только чтение" —
 * the text is never writable from here; comments are the owner's explicit opt-in per
 * share, enforced by the backend, never trusted from this UI. The document_id in the
 * URL is NOT a credential: knowing it never grants access (a live document_shares
 * row is still required). Referrer-Policy:no-referrer is injected on mount.
 *
 * ARCH (plan "public-share-reuse-readonly-layout"): deleted the parallel
 * TreeRow/RefCard/NoteCard/static-markdown render that previously lived here.
 * The page now reuses the real components — visual parity with authenticated
 * readonly is structural, not maintained by hand.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { AlertCircle, FileText, List, MessageSquare, Paperclip } from 'lucide-react';
import {
  publicTreeByDoc, publicDocumentByDoc, publicReferencesByDoc,
  type PublicTreeNode,
} from '../api/public-share';
import { ProjectShell, type ProjectShellHandle, type LeftTabEntry, type RightTabEntry } from '../components/ProjectShell';
import { PublicEditor } from '../components/PublicEditor';
import { EditorLinkPreview } from '../components/editor/EditorLinkPreview';
import { Sidebar } from '../components/Sidebar';
import { TableOfContents } from '../components/TableOfContents';
import { ReferencesPanel } from '../components/ReferencesPanel';
import { PublicNotesPanel } from '../components/PublicNotesPanel';
import SelectionToolbar from '../components/editor/SelectionToolbar';
import { usePublicNotesStore, PUBLIC_DRAFT_NOTE_ID } from '../store/public-notes-store';
import { PublicNoteConnector } from '../components/PublicNoteConnector';
import { usePublicDocSocket } from '../hooks/usePublicDocSocket';
import { useAppStore } from '../store/app-store';
import { useUIStore, ORPHAN_KEY, type RightTab } from '../store/ui-store';
import { setPublicFileContext } from '../utils/reference-url';
import { docUrl } from '../utils/routing';
import { useTranslation } from '../i18n';
import { Button } from '../components/ui';
import { useEvent } from '../hooks/useEvent';
import type { Document } from '../types';

type Phase = 'loading' | 'ready' | 'error';

/** Map a flat public-tree payload to the store's Document[] shape (metadata only —
 *  content is loaded per-doc via publicDocumentByDoc). */
function treeNodesToDocuments(nodes: PublicTreeNode[]): Document[] {
  const now = new Date().toISOString();
  return nodes.map(n => ({
    document_id: n.id,
    project_id: '',         // anonymous surface — no project context
    parent_id: n.parent_id,
    title: n.title,
    content: '',            // loaded per-doc
    path: '',
    is_index: false,
    sort_key: n.sort_key ?? undefined,
    created_at: now,
    updated_at: now,
  }));
}

/** `onTreeIds` is the authed-fallback seam: a signed-in visitor reaches this page
 *  through EditorRoutes when their own read was refused, and reporting the published
 *  tree's ids lets that route keep serving the public view as they walk the subtree
 *  (see publicFallbackDocIds in ui-store). Anonymous mounts omit it. */
export function PublicSharePage(
  { documentId, onTreeIds }: { documentId: string; onTreeIds?: (ids: string[]) => void },
) {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const setPublicShare = useUIStore(s => s.setPublicShare);
  const setPublicProjectName = useUIStore(s => s.setPublicProjectName);
  const setAccessLevel = useAppStore(s => s.setAccessLevel);
  const setDocuments = useAppStore(s => s.setDocuments);
  const setCurrentDocument = useAppStore(s => s.setCurrentDocument);
  const setReferences = useAppStore(s => s.setReferences);
  const hydrateReference = useAppStore(s => s.hydrateReference);

  const [phase, setPhase] = useState<Phase>('loading');
  const [error, setError] = useState<string>('');
  const [docFailed, setDocFailed] = useState(false);
  const [scope, setScope] = useState<'doc' | 'subtree'>('doc');

  const shellRef = useRef<ProjectShellHandle>(null);

  // ── Mount: flip the public-share switches ───────────────────────────────
  // (documentId-independent: runs once per page mount, not per doc-select.)
  useEffect(() => {
    // WHY the snapshot: in the authed-fallback mount (EditorRoutes serves this
    // page to a signed-in visitor whose own read was refused) the visitor returns
    // to authed surfaces on unmount — accessLevel must go back to what it was, or
    // a 'full' member keeps stale 'readonly' affordances until the next project
    // load re-derives it from my_access. Anonymous mounts restore the store
    // default, which the next authed cold path re-derives the same way.
    const prevAccessLevel = useAppStore.getState().accessLevel;
    setPublicShare(true);
    setAccessLevel('readonly');

    // Referrer-Policy: no-referrer for the public page — keeps the document id
    // out of Referer headers on any outbound navigation (plan risk). Removed on unmount.
    const meta = document.createElement('meta');
    meta.name = 'referrer';
    meta.content = 'no-referrer';
    document.head.appendChild(meta);

    return () => {
      setAccessLevel(prevAccessLevel);
      setPublicShare(false);
      setPublicProjectName(null);
      setPublicFileContext(null);
      usePublicNotesStore.getState().reset();
      document.head.removeChild(meta);
    };
  }, [setPublicShare, setAccessLevel, setPublicProjectName]);

  // ── Hydrate the store from the anonymous bundle ─────────────────────────
  const loadDocBundle = useCallback(async (docId: string) => {
    const [docResp, refs] = await Promise.all([
      publicDocumentByDoc(docId),
      publicReferencesByDoc(docId),
    ]);
    // Find the existing tree node to preserve parent_id; merge in content +
    // tables_json + headings from the public-document response.
    const existing = useAppStore.getState().documents.find(d => d.document_id === docId);
    const merged: Document = {
      document_id: docResp.document_id,
      project_id: '',
      parent_id: existing?.parent_id ?? null,
      title: docResp.title,
      content: docResp.content,
      path: '',
      is_index: false,
      sort_key: existing?.sort_key,
      tables_json: docResp.tables_json,
      headings: docResp.headings,
      created_at: existing?.created_at ?? new Date().toISOString(),
      updated_at: existing?.updated_at ?? new Date().toISOString(),
    };
    setCurrentDocument(merged);
    setReferences(refs);
    // The notes list is read against the text just committed: its anchors are
    // painted only while both carry the same content fingerprint.
    usePublicNotesStore.getState().setServed(docId, docResp.content_sha256);
    void usePublicNotesStore.getState().load(docId);
  }, [setCurrentDocument, setReferences]);

  // INVARIANT: the tree is fetched once per SHARE, not once per document. Why:
  // doc-select now changes the route, so this effect re-runs on every click inside
  // a published subtree; re-fetching the (identical) tree and dropping back to the
  // full-page 'loading' spinner turned an in-place content swap into a flash + an
  // extra round trip. A document already present in the loaded tree takes the
  // document-only path and never leaves the 'ready' phase.
  const treeDocIdsRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    let cancelled = false;
    const inLoadedTree = treeDocIdsRef.current?.has(documentId) ?? false;
    setDocFailed(false);
    (async () => {
      if (!inLoadedTree) setPhase('loading');
      try {
        if (!inLoadedTree) {
          const tree = await publicTreeByDoc(documentId);
          if (cancelled) return;
          // Seed the file-URL gate with the subtree ROOT document id (stable across
          // subtree navigation; resolve_share walks ancestors so one root covers all).
          setPublicFileContext(tree.root_id);
          setScope(tree.scope);
          setPublicProjectName(tree.project_name);
          setDocuments(treeNodesToDocuments(tree.nodes));
          treeDocIdsRef.current = new Set(tree.nodes.map(n => n.id));
          onTreeIds?.(tree.nodes.map(n => n.id));
        }
      } catch (e) {
        if (cancelled) return;
        // Tree stage: 404 (unpublished / unknown id) or 429 (rate-limited) — the SHARE
        // itself is unreadable, so the whole screen is the error. A failure inside a
        // loaded tree drops the cached id set: the next attempt re-resolves the share
        // instead of trusting a set that may be stale.
        treeDocIdsRef.current = null;
        setError(e instanceof Error ? e.message : t('publicShareFailed'));
        setPhase('error');
        return;
      }
      try {
        await loadDocBundle(documentId);
        if (!cancelled) setPhase('ready');
      } catch (e) {
        if (cancelled) return;
        // WHY: a per-document failure (backend 503 when the live Y.Doc can't be
        // captured) keeps the loaded tree on screen and clears currentDocument. Why:
        // replacing the whole page is right for a dead share, wrong for one transient
        // document — and leaving the PREVIOUS document rendered under the new URL is
        // exactly the stale-as-current the 503 exists to prevent. treeDocIdsRef stays
        // intact: the tree is not the thing that failed. References go with the
        // document: ReferencesPanel renders the store list without gating on
        // currentDocument, so keeping them would show the PREVIOUS document's
        // attachments beside the error — stale-as-current by another route.
        setCurrentDocument(null);
        setReferences([]);
        // The notes go with the document too — never the previous document's list.
        usePublicNotesStore.getState().reset();
        // The card shows the localized message; the raw detail (an English backend
        // string a reader can do nothing with) goes to the console for diagnosis.
        console.warn('public document load failed', documentId, e);
        setDocFailed(true);
        setPhase('ready');
      }
    })();
    return () => { cancelled = true; };
  }, [documentId, loadDocBundle, t, setDocuments, setPublicProjectName, setCurrentDocument, setReferences, onTreeIds]);

  // ── Doc-select within the share: CHANGE THE ROUTE (one id per document) ─
  // ARCH (plan "public-document-ids"): navigating to /docs/<childId> re-renders
  // this page with a new documentId prop → the hydrate effect above swaps the
  // document in place (the tree is already loaded — no spinner, no re-fetch).
  // Replaces the prior stay-on-/s/:token + loadDocBundle-no-route-change shape.
  useEvent('navigate-to-document', useCallback(({ documentId: docId }) => {
    navigate(docUrl(docId));
  }, [navigate]));

  // ── Open a reference within the share: NO route change (refs aren't routed) ─
  // publicReferencesByDoc returns rows via SELECT * (carries `content`), so
  // hydrateReference short-circuits without an authed GET — it just commits the
  // ref as currentReference. Media binaries route through the public file surface
  // (referenceFileUrl). PublicEditor renders the ref; back → setCurrentReference(null).
  useEvent('navigate-to-reference', useCallback(({ referenceId }) => {
    void hydrateReference(referenceId);
  }, [hydrateReference]));

  // ── Notes: the toolbar / Mod-m / a highlight click open the notes tab ────
  useEvent('open-notes', useCallback(({ threadId }: { threadId?: string }) => {
    // A click on the draft's own highlight just brings the composer back into view.
    if (threadId && threadId !== PUBLIC_DRAFT_NOTE_ID) usePublicNotesStore.getState().openNote(threadId);
    shellRef.current?.openRightPanel('notes');
  }, []));

  // ── Live notes/text: one anonymous change-ping socket per viewed document ──
  // The server sends {"type":"changed"} the moment a note or the text of THIS
  // document changes (members' and other guests' notes, REST/agent writes at
  // the next flush); the hook answers with the refetch the old 5 s poll ran.
  // The socket opens only while comments are on: the channel exists for notes.
  const allowComments = usePublicNotesStore(s => s.allowComments);
  usePublicDocSocket(documentId, allowComments);

  const storedRightTab = useUIStore(s => s.documents[ORPHAN_KEY]?.rightPanelTab);
  const noteThreadOpen = usePublicNotesStore(s => !!s.activeNoteId || !!s.draft);

  // A document that already has notes opens on the Notes tab — once per document,
  // so the reader's own switch back to References is not overridden by a refetch.
  const notesDocId = usePublicNotesStore(s => (s.notes.length > 0 ? s.documentId : null));
  const autoNotesDocRef = useRef<string | null>(null);
  useEffect(() => {
    if (!notesDocId || autoNotesDocRef.current === notesDocId) return;
    autoNotesDocRef.current = notesDocId;
    useUIStore.getState().setRightPanelTab(null, 'notes');
  }, [notesDocId]);

  // ── Render gates ─────────────────────────────────────────────────────────
  if (phase === 'loading') {
    return (
      <div className="h-dvh flex items-center justify-center bg-bg text-text">
        <div className="w-6 h-6 border-2 border-text-dim border-t-transparent animate-spin" />
      </div>
    );
  }

  if (phase === 'error') {
    return (
      <div className="h-dvh flex items-center justify-center bg-bg text-text">
        <div className="flex flex-col items-center gap-3 max-w-md text-center px-4">
          <AlertCircle size={32} className="text-text-dim" />
          <h1 className="text-lg font-medium">{t('publicShareNotFound')}</h1>
          <p className="text-ui-base text-text-dim">{error}</p>
          <Button variant="primary" onClick={() => navigate('/')}>{t('backToHome')}</Button>
        </div>
      </div>
    );
  }

  // ── Left-tab set: doc-scope hides the `docs` tab (single root only); subtree
  //    shows both. Right-tab set: refs only.
  const leftTabs: LeftTabEntry[] = [
    ...(scope === 'subtree' ? [{
      tab: 'docs' as const,
      icon: <FileText size={15} />,
      title: t('tabDocuments'),
      renderPanel: () => <Sidebar />,
    }] : []),
    { tab: 'toc', icon: <List size={15} />, title: t('tabTableOfContents'), renderPanel: () => <TableOfContents /> },
  ];

  const rightTabs: RightTabEntry[] = [
    { tab: 'refs', icon: <Paperclip size={15} />, title: t('tabReferences'), renderPanel: () => <ReferencesPanel /> },
    ...(allowComments ? [{
      tab: 'notes' as const,
      icon: <MessageSquare size={15} />,
      title: t('tabNotes'),
      renderPanel: () => <PublicNotesPanel />,
      // Same chrome as the member surface: the tab turns yellow while a note is open.
      className: noteThreadOpen ? 'notes-tab-thread' : undefined,
    }] : []),
  ];
  const effectiveRightTab: RightTab =
    rightTabs.some(e => e.tab === storedRightTab) ? storedRightTab as RightTab : 'refs';

  return (
    <ProjectShell
      ref={shellRef}
      leftTabs={leftTabs}
      rightTabs={rightTabs}
      renderCenter={() => (docFailed ? (
        <div className="h-full flex items-center justify-center bg-bg text-text">
          <div className="flex flex-col items-center gap-3 max-w-md text-center px-4 py-6 border border-border">
            <AlertCircle size={24} className="text-text-dim" />
            <p className="text-ui-base">{t('publicDocUnavailable')}</p>
          </div>
        </div>
      ) : <PublicEditor />)}
      userControlsVariant="minimal"
      rightPanelDocId={null}
      rightPanelReady={true}
      effectiveRightTab={effectiveRightTab}
      notesThreadOpen={noteThreadOpen}
      initialSidebarOpen={true}
      initialRightOpen={true}
      overlays={
        // EditorLinkPreview: global hover-preview host for cm-doc-link /
        // cm-ref-link inside the public editor. Mounted here (via overlays, the
        // same slot ProjectPage uses) so the popup floats above both the editor
        // and the panels. Resolves doc content via fetchDocumentContent, which
        // is document-keyed + public-aware (routes through /public/documents/{id}).
        // Ref content + image URLs come from the store (publicReferencesByDoc
        // SELECT * carries bodies; referenceFileUrl switches to the public file surface).
        // SelectionToolbar: the note-only bar for a guest while comments are on (it
        // renders nothing otherwise — the page is readonly).
        <>
          <EditorLinkPreview />
          <SelectionToolbar />
          <PublicNoteConnector open={effectiveRightTab === 'notes'} />
        </>
      }
    />
  );
}
