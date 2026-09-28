/** Global Zustand store: user session, project/document state, references. */
// ARCH: UI persistence delegated to ui-store (bridge pattern). Note state in note-store.
// ARCH: composed from slices (app-store/*-slice.ts), following chat-store and ui-store.
//       What stays in THIS file is deliberate, not leftovers: the entity pointers
//       (currentDocument/Reference/Table/preview) and the transitions that move them —
//       setCurrentProject and setCurrentDocument. Those two write across every slice at
//       once (clear the lists, reset presence, commit a prefetched ref list), and a
//       transition owned half by a slice is how one of its resets gets forgotten.
//       Each slice states what it does NOT own for the same reason.
// SYSTEM: app-store — global Zustand store (session, collections, references)

import { create } from 'zustand';
import { AccessLevel, User, Project, Document, DocumentTreeNode, Reference, Checkpoint, PresenceUser, HeadingItem } from '../types';
import { useNoteStore } from './note-store';
import { useUIStore, registerAppBridge } from './ui-store';
import { t } from '../i18n';
import { loadReferences, clearReferencesInFlight } from '../api/references-fetch';
import { resolveRestoredReference, isRestoreStub } from './restore-entity';
import { clearLastMessagePreviewCache } from '../hooks/useLastMessagePreview';
import { resetEditorHost } from '../components/editor/editor-host';
import { registerLogoutHandler } from './logout-handlers';
import { createCollabUsersSlice } from './app-store/collab-users-slice';
import { createSnapshotSlice } from './app-store/snapshot-slice';
import { createDocumentTreeSlice } from './app-store/document-tree-slice';
import { createSessionSlice } from './app-store/session-slice';
import {
  createReferencesSlice, clearHydrateInFlight, mergeRefLists, commitRefList,
  clearDroppedBodies, stashDroppedBodies, restoreDroppedBodies,
} from './app-store/references-slice';

// WHY: refs prefetch lives in setCurrentDocument so doc swap + restored
// reference commit in a single render — avoids "doc shown without ref, ref
// pops in" flicker when there is a persisted documents[docId].selectedReferenceId entry.

// Epoch of the latest setCurrentDocument ENTRY (a fresh navigation intent). The
// async doc-switch tails (restoreFlow, the URL-driven prefetch path) capture it and
// drop their commit when a newer intent superseded them — see INVARIANT(doc-desync)
// markers inside setCurrentDocument. Monotonic module state; never reset.
let docOpenEpoch = 0;

// INVARIANT: cross-store resets must both run and must not abort the project switch.
// Why: a throw in one store (note-store / ui-store) used to leave the others stale on
// switch, producing inconsistent UI. Each reset is isolated; a failure surfaces a toast
// (no silent degradation) rather than skipping the sibling reset.

function resetSiblingStores(isProjectSwitch: boolean): void {
  try {
    useNoteStore.getState().resetForProjectSwitch();
  } catch (e) {
    console.error('note-store resetForProjectSwitch failed', e);
    useAppStore.getState().showToast(t('projectSwitchNotesStale'), 'error');
  }
  try {
    useUIStore.getState().resetForProjectSwitch(isProjectSwitch);
  } catch (e) {
    console.error('ui-store resetForProjectSwitch failed', e);
    useAppStore.getState().showToast(t('projectSwitchPanelsStale'), 'error');
  }
  clearLastMessagePreviewCache();
  clearHydrateInFlight();
  clearDroppedBodies();
  resetEditorHost({ scope: 'project' });
}

// Register bridge so ui-store can read app-store context without circular imports
registerAppBridge({
  getAppContext: () => {
    const s = useAppStore.getState();
    return {
      currentUser: s.currentUser,
      currentProject: s.currentProject,
      currentDocument: s.currentDocument,
    };
  },
  showToast: (msg, type) => useAppStore.getState().showToast(msg, type),
});

// Register the references SWR cache clear into the logout
// registry. references-fetch is a leaf app-store already owns, so it can't import the
// store-layer registry itself — app-store registers on its behalf. The AI-chat and
// note caches self-register (sessions-slice / note-chat-store). setCurrentUser(null)
// fires them all (see clearUserScopedCaches). Why: a soft logout must drop the prior
// user's SWR caches so a same-tab re-login into a shared project can't flash them.
// Wrapped (not a bare reference) so the call resolves through the live binding at
// logout time — keeps the cache clear interceptable for the app-store wiring tests.
registerLogoutHandler(() => clearReferencesInFlight());
// Stashed reference bodies are user-scoped data, same as the SWR list above.
registerLogoutHandler(() => clearDroppedBodies());

// Re-exported so the module's import surface survived the W6 slice split:
// app-store.test.ts imports clearHydrateInFlight from HERE (11 call sites).
export { clearHydrateInFlight };

// ─── Store interface ────────────────────────────────────────────────────────

/**
 * The table-focus pointer (analogous to `currentReference` for the References-panel
 * "open table in center" flow). A table is NOT a separate entity — it is a CRDT subtree
 * of the document whose id is `document_id` — so the pointer is doc-scoped: it clears on a
 * document SWITCH (see `setCurrentDocument`) but survives a same-doc metadata refresh.
 *
 * ARCH: stores ONLY `{ document_id, table_id }` — NOT the label. The displayed label is
 * resolved LIVE from the document's `tables` list into `currentTableLabel` (maintained by
 * the ReferencesPanel), so a rename by the local user OR a collaborative peer never leaves a
 * stale label in the focus banner / header / split banner. Why: the label is the anchor
 * alt-text in `content`, a piece of collaborative state the pointer would otherwise snapshot.
 */
export interface CurrentTable {
  document_id: string;
  table_id: string;
}

export interface AppState {
  currentUser: User | null;
  currentProject: Project | null;
  currentDocument: Document | null;
  currentReference: Reference | null;
  currentTable: CurrentTable | null;
  /** LIVE label for the focused table (resolved reactively, see `setCurrentTableLabel`). */
  currentTableLabel: string | null;
  previewDocument: Document | null;
  previewScrollOffset: number | null;
  pendingReference: Reference | null;
  referenceSourceDocId: string | null;
  users: User[];
  projects: Project[];
  documents: Document[];
  documentTree: DocumentTreeNode[];
  references: Reference[];
  // Bumped when a document_moved event's new
  // OR previous host is the currently-shown document, so the References panel
  // re-fetches its per-host list (a moved reference changes WHERE it is listed).
  referencesReloadKey: number;

  accessLevel: AccessLevel;
  snapshotPreview: Checkpoint | null;
  snapshotModalOpen: boolean;
  snapshotPendingContent: string | null;
  snapshotPendingTablesJson: string | null;

  recording: {
    startedAt: number;
    targetDocumentId: string;
    targetDocumentTitle: string;
  } | null;

  pinLocked: boolean;
  setPinLocked: (locked: boolean) => void;

  // One-shot request: focus the editor rendering this entity id once it becomes
  // editable, whatever its role. Set after creating a reference so the user can
  // keep typing without clicking into it.
  pendingEditorFocus: string | null;
  setPendingEditorFocus: (entityId: string | null) => void;

  // SYSTEM: maxAttachmentMb single-source. The
  // shared chat-attachment budget (sum of all images, raw bytes), hydrated from
  // GET /chat/models. Previously lived in chat-store; lifted here so note-chat and
  // AI-chat both read ONE source. Default 5 (matching backend CHAT_MAX_IMAGE_SIZE_MB)
  // so attach-time validation works before loadModels resolves.
  maxAttachmentMb: number;
  setMaxAttachmentMb: (mb: number) => void;

  // Where a section page (/admin, /cabinet) was entered FROM — the SectionShell
  // back button navigates to `path` and labels itself `label`. Set at every
  // navigation INTO a section (UserControls); why a {path,label} object and not
  // the old lastEditorPath: the editor-only path pointed at an editor even when
  // the user came from /projects, so the button lied about its target.
  // fromProject carries the ONE inherited tab: entered from the editor shell,
  // the section's left rail shows the project's Documents tab as a return icon.
  // Why a boolean and not a tab array: only this tab is ever inherited (the TOC
  // tab was ruled out as noise), and a list would invite the section to render
  // project panels it must never mount (SectionShell is store-free by design).
  sectionOrigin: { path: string; label: string; fromProject: boolean } | null;
  setSectionOrigin: (origin: { path: string; label: string; fromProject: boolean } | null) => void;
  // The active section's label for the header crumb (`Admin panel : <section>`),
  // set by the section page from its OWN active tab — not from the persisted
  // ui-store choice, which the page may fall back from (a stale admin-only tab
  // for a moderator). In-memory only; the page clears it on unmount.
  sectionCrumb: string | null;
  setSectionCrumb: (label: string | null) => void;

  toast: { message: string; type: 'info' | 'error' | 'warning'; persistent?: boolean } | null;
  showToast: (message: string, type?: 'info' | 'error' | 'warning', options?: { persistent?: boolean }) => void;
  clearToast: () => void;

  // Data setters
  setUsers: (users: User[]) => void;
  setProjects: (projects: Project[]) => void;
  setDocuments: (documents: Document[]) => void;
  setReferences: (refs: Reference[]) => void;
  mergeReferences: (refs: Reference[]) => void;
  addReference: (ref: Reference) => void;
  removeReference: (id: string) => void;
  updateReference: (id: string, patch: Partial<Reference>) => void;
  replaceReference: (tempId: string, ref: Reference) => void;
  bumpReferencesReload: () => void;

  // WHY: deletedRefIds is applied to the `references` list ONLY via commitRefList
  // Why: only server-snapshot merges must honor deletes; direct assignments own their list.
  // (honorDeleted=true), which today is only mergeReferences — never setReferences/add/
  // remove (honorDeleted=false). Filtering those breaks save-flow rollbacks and direct
  // list assignments. See commitRefList for the full rationale.
  deletedRefIds: Set<string>;
  addRefToDeleting: (id: string) => void;
  removeRefFromDeleting: (id: string) => void;

  pendingUploadRefIds: Set<string>;
  addPendingUploadRefIds: (ids: string[]) => void;
  removePendingUploadRefIds: (ids: string[]) => void;

  // Current item setters
  setCurrentUser: (user: User | null) => void;
  setCurrentProject: (project: Project | null) => void;
  setCurrentDocument: (
    doc: Document | null,
    prefetch?: { references: Reference[]; restoredReference: Reference | null },
  ) => void;
  setCurrentReference: (ref: Reference | null) => void;
  /**
   * Open the DOCUMENT body (the default `openDocument` path — no `restore`).
   * Same doc ⇒ unfocus the focused reference/table in place, return 'stayed' (the
   * caller does NOT re-navigate — references have no route, so the URL is already
   * the doc's). Any other doc ⇒ clear the destination's remembered per-doc reference
   * pointer, return 'navigate' — the caller's setCurrentDocument restoreFlow then
   * finds no pointer and opens the bare document instead of restoring its
   * last-opened reference.
   */
  focusDocument: (docId: string) => 'stayed' | 'navigate';
  setCurrentTable: (table: CurrentTable | null) => void;
  /** Set the LIVE label for the focused table (written by the ReferencesPanel effect). */
  setCurrentTableLabel: (label: string | null) => void;
  setPendingReference: (ref: Reference | null) => void;
  setPreviewDocument: (doc: Document | null, scrollOffset?: number | null) => void;
  clearPreviewDocument: () => void;

  /**
   * GET the full Reference (content + headings) for a list-sourced ref and commit it
   * to the store list + currentReference. Used by every opener that turns a panel ref
   * (metadata-only LIST — no content) into the active editor entity.
   *
   * ARCH: the /api/references LIST is metadata-only; the single-ref GET is the
   * lazy-fetch endpoint. No-empty-flash: the list ref is committed as currentReference
   * immediately (a content-less ref with has_content shows the editor loading state),
   * THEN the body is hydrated. Image refs and already-hydrated refs short-circuit.
   *
    * INVARIANT: concurrent calls for the same id collapse to ONE GET (hydrateInFlight),
    * Why: dedup avoids redundant fetches; hydrateLastId prevents a late resolve from yanking selection.
    * and only the most-recently-requested id may promote currentReference on resolve
   * (hydrateLastId) — so a late-settling fetch never yanks the selection back. Both
   * are cleared on project switch.
   */
  hydrateReference: (refOrId: Reference | string) => Promise<Reference | null>;

  // WHY: liveHeadings is an EPHEMERAL override of the server-computed `headings`
  // field, derived client-side from the live CM6 doc by live-headings-extension.
  // app-store has NO persist middleware (UI persistence is delegated to ui-store),
  // so it stays in-memory by construction — never add it to a partialize/localStorage
  // list. Reset on entity switch so a fresh open never flashes the previous entity's
  // headings before the extension repopulates.
  liveHeadings: { entityId: string; items: HeadingItem[] } | null;
  setLiveHeadings: (entityId: string, items: HeadingItem[]) => void;

  // Connected users in the active collab entity (presence chips). Reset on entity switch.
  collabUsers: PresenceUser[];
  setCollabUsers: (users: PresenceUser[]) => void;
  addCollabUser: (user: PresenceUser) => void;
  removeCollabUser: (userId: string) => void;

  // Other setters
  setAccessLevel: (v: AccessLevel) => void;
  setSnapshotPreview: (cp: Checkpoint | null) => void;
  openSnapshotModal: (pendingContent: string, pendingTablesJson?: string | null) => void;
  closeSnapshotModal: () => void;
  setRecording: (r: AppState['recording']) => void;
}

// ─── Store implementation ───────────────────────────────────────────────────

export const useAppStore = create<AppState>((set, get) => ({
  ...createSessionSlice(set),
  currentProject: null,
  currentDocument: null,
  currentReference: null,
  currentTable: null,
  currentTableLabel: null,
  previewDocument: null,
  previewScrollOffset: null,
  pendingReference: null,
  referenceSourceDocId: null,
  ...createDocumentTreeSlice(set, get),
  ...createReferencesSlice(set, get),
  ...createCollabUsersSlice(set),

  // INVARIANT(security): three-tier (full/commentator/readonly); must never be undefined when project is selected. Why: undefined access would let UI show editor triggers the backend rejects.
  accessLevel: 'full',
  ...createSnapshotSlice(set),
  // INVARIANT: non-null only during active recording; must null on project switch. Why: a stale recording handle would keep writing frames into the wrong project after a switch.
  recording: null,
  sectionOrigin: null,
  sectionCrumb: null,
  pendingEditorFocus: null,
  // L1: default 5 MB before /chat/models resolves (matches backend CHAT_MAX_IMAGE_SIZE_MB).
  maxAttachmentMb: 5,
  liveHeadings: null,

  setPendingEditorFocus: (entityId) => set({ pendingEditorFocus: entityId }),
  // L1: shared attachment budget default. The
  // /chat/models fetch overrides it via setMaxAttachmentMb (chat-store.misc-slice).
  setMaxAttachmentMb: (mb) => set({ maxAttachmentMb: mb }),
  setSectionOrigin: (origin) => set({ sectionOrigin: origin }),
  setSectionCrumb: (label) => set({ sectionCrumb: label }),

  // Data setters
  // Current item setters
  setCurrentProject: (project) => {
    const prev = get();
    useUIStore.getState().saveUIStateNow();

    if (!project) {
      resetSiblingStores(true);
      return set({
        currentProject: null, accessLevel: 'readonly',
        currentDocument: null, currentReference: null, currentTable: null, currentTableLabel: null, pendingReference: null, referenceSourceDocId: null,
        previewDocument: null, previewScrollOffset: null,
        documents: [], documentTree: [], references: [], referencesReloadKey: 0,
        recording: null, snapshotPreview: null,
      });
    }

    if (prev.currentProject?.project_id === project.project_id) {
      return set({ currentProject: project });
    }

    // WHY: Only reset collections when switching FROM one project TO another.
    const isProjectSwitch = prev.currentProject !== null;

    set({
      currentProject: project,
      ...(isProjectSwitch ? {
        accessLevel: 'readonly',
        currentDocument: null,
        currentReference: null,
        currentTable: null,
        currentTableLabel: null,
        pendingReference: null,
        referenceSourceDocId: null,
        previewDocument: null,
        previewScrollOffset: null,
        documents: [],
        documentTree: [],
        references: [],
        referencesReloadKey: 0,
        snapshotPreview: null,
      } : {}),
      recording: null,
    });

    resetSiblingStores(isProjectSwitch);

    const userId = prev.currentUser?.user_id;
    if (userId) {
      useUIStore.getState().loadProjectPrefs(userId, project.project_id);
    }
  },
  // WHY: setCurrentDocument/setCurrentReference reset snapshotPreview to null;
  // setCurrentDocument to a DIFFERENT doc also resets previewDocument.
  // Why: a preview must never survive navigation, but a same-doc metadata refresh
  // must not dismiss an open preview.
  // Save paths must NOT use these — use content-sync::syncToStore instead.
  // WHY: a URL-driven document open commits the document TOGETHER with its
  // Why: a ref-less commit patched in later was a visible flicker (see detail below).
  // restored reference (and references list) in a single render — the caller
  // (DocumentPage) fetches the sub-state in parallel with the document and hands it
  // in via `prefetch`. Why: committing the doc ref-less and patching the reference in
  // later was a visible flicker (this is a repeatedly-regressing area; see WHY note
  // at top of file). The restoreFlow fallback below serves only non-prefetching
  // in-app callers (DocumentTree/Header).
  setCurrentDocument: (doc, prefetch) => {
    const noteState = useNoteStore.getState();
    // INVARIANT(doc-desync): every setCurrentDocument entry bumps the navigation epoch.
    // Why: the async doc-switch tails below must drop commits superseded by a newer
    // intent — a stale late commit rewound the store behind the URL (gray drive
    // 2026-08-25: rapid tree switching left the editor on the previous doc's restored
    // reference while the URL pointed at the clicked doc, until the next user action).
    const openEpoch = ++docOpenEpoch;
    const pending = noteState.pendingNoteNavigation;
    const activeThreadId = pending
      ? pending.noteId
      : (doc ? noteState.documentNoteThreads[doc.document_id] || null : null);

    const { pendingReference, currentProject } = get();
    const applyRef = pendingReference && doc && pendingReference.document_id === doc.document_id
      ? pendingReference : null;

    const commit = (restoredRef: Reference | null, refs?: Reference[]) => {
      // An id-only restore stub (panel quick preview of a cross-doc reference: the saved
      // id is not in the doc's list) has no title/media_type to render — commit the
      // document ALONE and let the id-only hydrate below promote the reference on
      // resolve (latest-wins via hydrateLastId, so a later open is never yanked).
      const restoreStubId = restoredRef && isRestoreStub(restoredRef) ? restoredRef.reference_id : null;
      const docChanged = doc?.document_id !== get().currentDocument?.document_id;
      // Field-preserving merge (mergeRefLists) so refs shared across docs — index/ancestor
      // refs are in every scope — keep their hydrated bodies instead of being wiped to
      // content-less on every doc switch (the LIST is metadata-only). honorDeleted=true:
      // a just-deleted ref must not resurrect on the new scope. Computed BEFORE the set so
      // the doc + reference + references commit stays atomic (one render, no flicker).
      const { references: prevRefs, deletedRefIds } = get();
      const nextRefs = refs
        ? commitRefList(mergeRefLists(restoreDroppedBodies(refs), prevRefs), true, deletedRefIds)
        : undefined;
      // Bodies leaving with the old scope are stashed so returning to it does not re-fetch.
      if (nextRefs) stashDroppedBodies(prevRefs, nextRefs);
      // INVARIANT: the restored reference is committed as its MERGED list row, never the
      // raw metadata-only row. Why: an in-app open commits the same doc twice (Header's
      // optimistic open, then DocumentPage's prefetch); the raw row dropped the body the
      // first commit had hydrated, so the open reference flashed "Loading…" and remounted.
      const restoredRow = restoreStubId || !restoredRef
        ? null
        : nextRefs?.find(r => r.reference_id === restoredRef.reference_id) ?? restoredRef;
      const finalRef = applyRef ?? restoredRow;
      set({
        currentDocument: doc,
        currentReference: finalRef,
        pendingReference: null,
        snapshotPreview: null,
        liveHeadings: null,
        // currentTable is doc-scoped: clear only on a real doc CHANGE (a same-doc metadata
        // refresh must keep an open table focused), mirroring previewDocument below.
        ...(docChanged ? { previewDocument: null, previewScrollOffset: null, currentTable: null, currentTableLabel: null } : {}),
        ...(nextRefs ? { references: nextRefs } : {}),
      });
      // The search-tab overlay is doc-scoped: a doc switch drops it so returning
      // to the doc restores the user's real saved tab, not search.
      if (docChanged) useUIStore.getState().clearSearchTabOverlay();
      if (doc) {
        if (applyRef) {
          useUIStore.getState().setCurrentReferenceForDoc(doc.document_id, applyRef.reference_id);
        }
        useUIStore.getState().setMainEntity(
          doc.document_id,
          finalRef
            ? { type: 'reference', id: finalRef.reference_id }
            : { type: 'document', id: doc.document_id },
        );
      }
      // WHY: do NOT pass docId here. Why: setCurrentDocument flow only routes
      // thread focus; per-doc map ownership belongs to NotesPanel / useNoteCrud /
      // DocumentPage call sites. Passing docId would start populating the map for
      // paths that intentionally do not.
      useNoteStore.getState().setActiveNoteThreadId(activeThreadId);

      // Owns the post-commit body hydrate for BOTH the prefetch and restoreFlow paths.
      // Hydrates the ACTUAL winner (finalRef = applyRef ?? restoredRef), never
      // restoredReference blindly — a cross-doc link click sets pendingReference, which
      // wins via applyRef; hydrating restoredReference here would yank currentReference
      // off the clicked target (root cause of the ref-link revert — see
      // app-store.test.ts "a pending cross-doc reference wins the commit…"). Image refs
      // and already-hydrated refs short-circuit inside hydrateReference (no GET).
      // ARCH: this is the SINGLE owner of the post-commit hydrate — DocumentPage and
      // the restoreFlow below no longer hydrate separately (they used to hydrate
      // restoredReference, which yanked the pending winner).
      if (finalRef && finalRef.content === undefined && finalRef.media_type !== 'image') {
        void get().hydrateReference(finalRef);
      } else if (!finalRef && restoreStubId && doc) {
        // A stub whose reference no longer exists (deleted since the preview) is a
        // stale pointer: clear it so the next open does not re-fetch and re-toast.
        void get().hydrateReference(restoreStubId).then(r => {
          if (!r) useUIStore.getState().setCurrentReferenceForDoc(doc.document_id, null);
        });
      }
    };

    // Prefetched (URL-driven open): sub-state already fetched in parallel — commit
    // doc + restored reference + references list atomically, no second waterfall stage.
    if (prefetch) {
      // INVARIANT(doc-desync): a prefetch commit is dropped when the store already
      // shows a DIFFERENT doc (cur=null is the cold open, cur=doc the same-doc refresh).
      // Why: the DocumentPage effect's `cancelled` flag flips in React's cleanup, which
      // can run AFTER its fetch continuation — a prefetch committed in that window
      // rewinds the store to an abandoned navigation (measured on gray: a beta prefetch
      // committed while cur=alpha and the URL was already /docs/alpha).
      const cur = get().currentDocument?.document_id;
      if (!cur || cur === doc?.document_id) {
        commit(prefetch.restoredReference, prefetch.references);
      }
      return;
    }

    // Fast path: no doc, no project, or pendingRef already supplied — commit immediately.
    // (!currentProject is retained for non-prefetch callers: restoreFlow needs the
    // project's index_doc_id, and there is nothing to restore without a project.)
    if (!doc || !currentProject || applyRef) {
      commit(null);
      return;
    }

    // Saved-ref restore path: await any in-flight project prefs (handles F5 race
    // where doc fetch resolves before loadProjectPrefs hydrates the map), then
    // prefetch refs and commit doc + currentReference + references atomically.
    const restoreFlow = async () => {
      await useUIStore.getState().awaitProjectPrefs(currentProject.project_id);
      // Bail if user moved on to another project while we waited.
      if (get().currentProject?.project_id !== currentProject.project_id) return;

      // INVARIANT(doc-desync): drop this flow's commit when a newer setCurrentDocument
      // intent superseded this open AND the store does not already show this doc.
      // Why: identity alone is blind to A→B→A (cur equals the doc this flow started
      // from); the identity escape keeps the legit late same-doc refresh alive — it
      // must keep an open preview (see the previewDocument WHY note above).
      const superseded = () =>
        docOpenEpoch !== openEpoch && get().currentDocument?.document_id !== doc?.document_id;

      if (!useUIStore.getState().getCurrentReferenceForDoc(doc.document_id)) {
        if (!superseded()) commit(null);
        return;
      }
      try {
        // INVARIANT: share the panel fetch's in-flight dedup — on an in-app doc switch
        // Why: both GETs fire for the same (project, doc) in one tick; dedup avoids a double-blink.
        // this restore GET and ReferencesPanel's scope GET fire for the same
        // (project, doc) within the same tick; collapsing them to ONE round-trip is
        // what keeps the switch a single panel load (no double-blink). index_doc_id is
        // no longer passed: the server resolves the project index doc itself.
        // Why: see api/references-fetch.ts.
        const refs: Reference[] = await loadReferences(
          currentProject.project_id, doc.document_id,
        );
        const restored = resolveRestoredReference(doc.document_id, refs);
        if (!superseded()) commit(restored, refs);
      } catch (err) {
        // Graceful fallback: the doc still renders ref-less. But do NOT swallow
        // the failure silently — tell the user the references failed to load
        // (parity with hydrateReference's failedToFetchReference toast).
        console.error('Failed to load references on doc switch:', err);
        get().showToast(t('failedToLoadReferences'), 'error');
        if (!superseded()) commit(null);
      }
    };
    restoreFlow();
  },
  setCurrentReference: (ref) => {
    // ARCH: a reference focus and a table focus are mutually exclusive center views.
    // Opening a reference clears any focused table (and vice versa in setCurrentTable).
    set({ currentReference: ref, snapshotPreview: null, liveHeadings: null, ...(ref ? { currentTable: null, currentTableLabel: null } : {}) });
    const docId = get().currentDocument?.document_id;
    if (docId) {
      useUIStore.getState().setCurrentReferenceForDoc(docId, ref?.reference_id ?? null);
      useUIStore.getState().setMainEntity(
        docId,
        ref ? { type: 'reference', id: ref.reference_id } : { type: 'document', id: docId },
      );
    }
  },
  // ARCH: the unfocus-vs-navigate decision for "open the document body" has ONE owner —
  // this action — called by openDocument's default path for every non-restoring
  // source (the tree passes restore and skips it). 'stayed' = the caller must NOT
  // re-navigate (the URL is already the doc's — references have no route); 'navigate'
  // = run the normal setCurrentDocument + docUrl path.
  focusDocument: (docId) => {
    const cur = get().currentDocument;
    if (cur?.document_id !== docId) {
      // Cross-doc: "I chose the document" — clear the remembered per-doc pointer so
      // the following setCurrentDocument restoreFlow opens the bare document (parity
      // with the same-doc setCurrentReference(null) below, which clears it too).
      useUIStore.getState().setCurrentReferenceForDoc(docId, null);
      return 'navigate';
    }
    // Back from a focused reference/table to its document: clear the focus
    // instead of re-navigating. Tables and references are mutually exclusive.
    if (get().currentTable) {
      get().setCurrentTable(null);
      return 'stayed';
    }
    if (get().currentReference) {
      get().setCurrentReference(null);
      return 'stayed';
    }
    // Same doc, nothing focused: fall through so the existing navigate path (URL
    // sync + commit) still runs — do not short-circuit it.
    return 'navigate';
  },
  // ARCH: a table focus is a CRDT subtree of the current document, not a separate entity,
  // so it does NOT touch ui-store's per-doc main-entity map (notes/chat/TOC keep operating
  // on the parent document). It only clears snapshotPreview — a table focus and a snapshot
  // preview are mutually exclusive center views. currentTable clears on document switch
  // (see setCurrentDocument commit()) and on table deletion (panel/badge handler). Opening
  // a table clears any focused reference (mutual exclusivity, mirror of setCurrentReference).
  setCurrentTable: (table) => set({
    currentTable: table,
    snapshotPreview: null,
    // Closing the table focus clears the live label (opening a different table leaves the
    // label for the ReferencesPanel effect to repopulate reactively from the tables list).
    ...(table ? { currentReference: null } : { currentTableLabel: null }),
  }),
  setCurrentTableLabel: (label) => set({ currentTableLabel: label }),
  setPendingReference: (ref) => set({ pendingReference: ref }),
  setPreviewDocument: (doc, scrollOffset) => set({ previewDocument: doc, previewScrollOffset: scrollOffset ?? null }),
  clearPreviewDocument: () => set({ previewDocument: null, previewScrollOffset: null }),
  setLiveHeadings: (entityId, items) => set({ liveHeadings: { entityId, items } }),

  setAccessLevel: (v) => set({ accessLevel: v }),
  setRecording: (r: AppState['recording']) => set({ recording: r }),
}));
