/** Editor event listeners: navigate-to-reference, scroll-to-note. */
// ARCH: All useEvent listeners extracted from Editor — reduces useEffect density.
// ARCH: scroll-to-line lives in useScrollToLine (shared with the public viewer).

import { useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { EditorView } from '@codemirror/view';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { useNoteStore } from '../store/note-store';
import { useEvent } from './useEvent';
import type { EventMap } from '../events/event-types';
import { useScrollToLine } from './useScrollToLine';
import { firstVisibleLine, positionCacheSet } from '../editor/position-cache';
import { resolveReferenceNav } from './resolveReferenceNav';
import { refIsScope } from '../store/ui-store/documents-slice';
import { openDocument } from '../navigation/open-document';

interface UseEditorEventsParams {
  editorViewRef: React.RefObject<EditorView | null>;
  currentDocument: { document_id: string } | null;
  // WHY: false on the split-view reference (secondary) column — these listeners
  // target the document only, and binding them twice would double-fire navigate /
  // mutate the reference's content on note-link removal.  Why: these listeners target the document; binding them on the split-view reference column too would double-fire navigate and mutate the reference's content.
  enabled?: boolean;
}

export function useEditorEvents({ editorViewRef, currentDocument, enabled = true }: UseEditorEventsParams) {
  const navigate = useNavigate();
  // Scroll-to-line event from TOC — shared hook (also mounted by the public viewer).
  useScrollToLine({ editorViewRef, enabled });

  // ARCH: Centralized navigate-to-reference handler — fetches ref by ID, navigates to parent doc if needed.
  // WHY: Uses pendingReference to survive DocumentPage's async setCurrentDocument call.
  // WHY: When the ref is already in the store (typical case — click on a card in the open panel),
  // commit it synchronously. The blocking REST round-trip caused a visible pause before opening.  Why: pendingReference survives the async setCurrentDocument so a ref click isn't lost; if the ref is already in the store it commits synchronously to skip the pause.
  useEvent('navigate-to-reference', useCallback(async ({ referenceId, scrollToOffset, ...options }: EventMap['navigate-to-reference']) => {
    if (!enabled) return;
    useAppStore.getState().clearPreviewDocument();
    const view = editorViewRef.current;
    const docId = currentDocument?.document_id;
    if (view && docId) {
      const { from, offset } = firstVisibleLine(view);
      positionCacheSet(docId, { cursor: view.state.selection.main.head, scroll: from, scrollOffset: offset });
    }

    // ARCH: the references LIST is metadata-only (no content), so a ref found in the
    // store must be HYDRATED via the single-ref GET before it can drive the editor /
    // TOC / notes anchors. hydrateReference commits the list ref immediately (loading
    // state) then fills the body. The id-only branch covers refs not in the store.
    const store = useAppStore.getState();
    const inStore = store.references.find(r => r.reference_id === referenceId) ?? null;
    const ref = inStore
      ? await store.hydrateReference(inStore)
      : await store.hydrateReference(referenceId);
    if (!ref) return;

    if (scrollToOffset && view) {
      setTimeout(() => {
        const v = editorViewRef.current;
        if (v) {
          v.dispatch({ effects: EditorView.scrollIntoView(scrollToOffset, { y: 'center' }) });
        }
      }, 100);
    }

    // ARCH: routing decision extracted to resolveReferenceNav (pure, unit-tested) —
    // the cross-doc vs same-doc classification is the crux of the ref-link revert
    // The hook keeps ONLY the side effects; the decision is
    // testable in isolation (see resolveReferenceNav.test.ts).
    // The mode of the CURRENT document rides along: in 'panel' (quick preview)
    // every ref click stays in the document's Refs tab — the resolver owns that
    // decision, senders are untouched.
    const refOpenMode = useUIStore.getState().getRefOpenMode(docId ?? '');
    const decision = resolveReferenceNav({
      ref,
      currentDocId: docId,
      stayInContext: !!options.stayInContext,
      refOpenMode,
    });
    // INVARIANT: in 'panel' (quick preview) the Refs tab is revealed HERE, on the
    // user's open, and never by watching currentReference.
    // Why: a restored reference (F5, returning to the document) also sets
    // currentReference — a state-driven reveal overwrote the tab the user was on
    // (Chat) with Refs. The tab the user left is always what comes back.
    if (docId && !refIsScope(refOpenMode)) useUIStore.getState().pinRightPanel(docId, 'refs');
    // noop: ref was null (hydrate resolved to nothing).
    // stay-in-context: populate the ref column without navigating.
    // same-doc: no side effects — hydrateReference (called above) already set
    //   currentReference for BOTH the in-store path (immediate marker + guarded
    //   resolve) and the id-only path (guarded resolve). A redundant unguarded set
    //   here would bypass the store's hydrateLastId latest-wins guard and yank the
    //   selection back on a rapid double-click (root cause 2 of the ref-link revert).
    if (decision.kind !== 'cross-doc') return;

    // Cross-doc branch: ref is owned by a different (often ancestor) document.
    // The set here is idempotent and required for the setPendingReference + navigate
    // side effects below (the winner then survives setCurrentDocument's commit via
    // applyRef — see app-store.ts commit()).
    const s = useAppStore.getState();
    useAppStore.setState({ currentReference: ref, snapshotPreview: null });
    s.setPendingReference(ref);
    useAppStore.setState({ referenceSourceDocId: options.sourceDocId === undefined ? (currentDocument?.document_id ?? null) : options.sourceDocId });
    const project = s.currentProject;
    if (project) {
      // WHY: the target document is committed BEFORE navigate — through the ONE
      // owner, openDocument (SYSTEM: document-navigation; the ordering
      // INVARIANT lives there). Why: DocumentPage's URL-driven prefetch commit
      // is DROPPED when the store shows a different doc (INVARIANT(doc-desync)
      // in app-store.setCurrentDocument), so a bare navigate here changed the
      // URL and left the previous document on screen. The pending reference set
      // above makes this the fast path: it commits synchronously with the
      // reference as the winner (lessons/2026-09-17-guard-on-a-shared-commit-path
      // -needs-every-writer-driven.md).
      openDocument(decision.targetDocId, { navigate });
    }
  }, [editorViewRef, currentDocument?.document_id, navigate, enabled]));

  // Scroll to note text in document when a note card is clicked
  useEvent('scroll-to-note-in-editor', useCallback(({ noteId }: { noteId: string }) => {
    if (!enabled) return;
    if (!editorViewRef.current) return;
    const view = editorViewRef.current;
    const docText = view.state.doc.toString();
    const idx = docText.indexOf(`note:${noteId}`);
    if (idx !== -1) {
      view.dispatch({ effects: EditorView.scrollIntoView(idx, { y: 'center' }) });
    }
  }, [editorViewRef, enabled]));

  useEvent('connect-note', useCallback(({ noteId }: { noteId: string }) => {
    if (!enabled) return;
    useNoteStore.getState().setConnectedNoteId(noteId);
  }, [enabled]));
}

import type React from 'react';
