/** Note CRUD operations via chat sessions API + real-time collab event listeners. */
// ARCH: Bridges note UI to chat sessions API (is_note=true). Uses note-chat-store
// for session state instead of stale /documents/{id}/notes endpoints.
// INVARIANT: root list — newest first; thread — chronological (newest last).  Why: the root list shows newest sessions first; a thread is chronological (newest last) so the conversation reads top-to-bottom.
// ARCH: Always loads from parent document_id — backend returns doc + ref sessions.
// Client-side groups by reference_id for document mode, filters for ref mode.

import { useEffect, useCallback, useMemo } from 'react';
import { useAppStore } from '../store/app-store';
import { useNoteStore } from '../store/note-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { useUIStore, useDocState } from '../store/ui-store';
import { readRefOpenMode, showsBothPanes, refIsScope } from '../store/ui-store/documents-slice';
import { emit } from '../events';
import { useEvent } from './useEvent';
import type { ChatSession } from '../types';

interface UseNoteCrudParams {
  highlightNote: (noteId: string) => void;
}

/**
 * Pure predicate for the split-mode notes list (extracted for testability — the hook
 * memo itself is covered by manual testing per testing.md).
 *
 * In split view BOTH the document (left) and the open reference (right) are visible,
 * so the merged list shows every document note (reference_id === null) plus notes
 * attached to the OPEN reference, and excludes notes of any OTHER reference.
 */
export function selectSplitNoteItems(items: ChatSession[], openRefId: string): ChatSession[] {
  return items.filter(s => s.reference_id == null || s.reference_id === openRefId);
}

export function useNoteCrud({ highlightNote }: UseNoteCrudParams) {
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  const currentProject = useAppStore(s => s.currentProject);
  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);
  const setActiveNoteThreadId = useNoteStore(s => s.setActiveNoteThreadId);
  const pendingNav = useNoteStore(s => s.pendingNoteNavigation);

  const noteSessions = useNoteChatStore(s => s.sessions);
  const sessionsLoading = useNoteChatStore(s => s.sessionsLoading);
  const activeNoteSessionId = useNoteChatStore(s => s.activeSessionId);
  const loadSessions = useNoteChatStore(s => s.loadSessions);
  const deleteSession = useNoteChatStore(s => s.deleteSession);

  const refOpenMode = readRefOpenMode(useDocState(currentDocument?.document_id ?? null), useUIStore(s => s.compactLayout));
  // Panel quick preview: notes are the DOCUMENT's (decision A — no notes on the
  // preview). Reading the open reference through the ONE projection drops both
  // isRefMode and isSplitMode to their document values while a ref is previewed.
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  const isRefMode = !!currentReference;
  // WHY: split mode shows BOTH columns, so the
  // notes list merges document + open-reference notes. Discriminator must NOT
  // collapse onto isRefMode (which is true in single-editor-with-reference too,
  // where the document is hidden and its notes have no visible anchor target).
  const isSplitMode = !!currentReference && !!currentDocument && showsBothPanes(refOpenMode);

  useEffect(() => {
    if (!currentProject || !currentDocument) {
      useNoteChatStore.getState().reset();
    }
  }, [currentProject, currentDocument]);

  useEffect(() => {
    if (activeNoteThreadId) {
      const store = useNoteChatStore.getState();
      const session = store.sessions.find(s => s.session_id === activeNoteThreadId);
      if (session && store.activeSessionId !== activeNoteThreadId) {
        store.setActiveSession(activeNoteThreadId);
      }
    }
  }, [activeNoteThreadId]);

  useEvent('highlight-note', useCallback(({ noteId }: { noteId: string }) => {
    highlightNote(noteId);
  }, [highlightNote]));

  useEvent('create-note-from-editor', useCallback(({ noteId }: { noteId: string }) => {
    const store = useNoteChatStore.getState();
    const session = store.sessions.find(s => s.session_id === noteId);
    if (session) {
      useNoteStore.getState().setActiveNoteThreadId(noteId, currentDocument?.document_id);
      store.setActiveSession(noteId);
    }
  }, [currentDocument?.document_id]));

  useEffect(() => {
    if (!pendingNav) return;
    const store = useNoteChatStore.getState();
    const session = store.sessions.find(s => s.session_id === pendingNav.noteId);
    if (!session) return;
    useNoteStore.getState().setPendingNoteNavigation(null);
    useNoteStore.getState().setActiveNoteThreadId(pendingNav.noteId, currentDocument?.document_id);
    store.setActiveSession(pendingNav.noteId);
    emit('scroll-to-note-in-editor', { noteId: pendingNav.noteId });
    if (session.anchor_rel_start != null || session.anchor_offset_start != null) {
      useNoteStore.getState().setConnectedNoteId(pendingNav.noteId);
    }
  }, [noteSessions, currentDocument?.document_id, pendingNav]);

  useEvent('document-deleted', useCallback(() => {
    if (currentProject && currentDocument) {
      loadSessions(currentProject.project_id, currentDocument.document_id);
    }
  }, [currentProject?.project_id, currentDocument?.document_id, loadSessions]));

  // See SYSTEM: note-realtime — apply best-effort remote
  // nudges idempotently. The actor's own client also receives the broadcast frame and
  // dedups by id (store helpers). Events are nudges, not state — receivers reconcile on
  // the next loadSessions (scope change) if a frame is missed.
  useEvent('collab-note-created', useCallback((session: { session_id: string; [k: string]: unknown }) => {
    const store = useNoteChatStore.getState();
    // Idempotent: the actor's optimistic prepend already holds this id → skip.
    if (store.sessions.some(s => s.session_id === session.session_id)) return;
    // Minimal frame (system-note path: session_id only, no preview fields) → the note's
    // display is derived server-side, so refresh the list rather than upsert an empty card.
    const isFullSession = session.is_note !== undefined
      || session.first_message_preview !== undefined
      || session.created_at !== undefined
      || session.updated_at !== undefined;
    if (!isFullSession) {
      if (currentProject && currentDocument) {
        void store.loadSessions(currentProject.project_id, currentDocument.document_id);
      }
      return;
    }
    store.upsertSession(session as unknown as Parameters<typeof store.upsertSession>[0]);
  }, [currentProject?.project_id, currentDocument?.document_id]));

  useEvent('collab-note-updated', useCallback((payload: { action: string; session_id: string; [k: string]: unknown }) => {
    type ApplyMsg = Parameters<ReturnType<typeof useNoteChatStore.getState>['applyMessageChange']>[0];
    useNoteChatStore.getState().applyMessageChange(payload as unknown as ApplyMsg);
  }, []));

  useEvent('collab-note-deleted', useCallback(({ sessionId }: { sessionId: string }) => {
    useNoteChatStore.getState().removeSession(sessionId);
  }, []));

  const handleDelete = useCallback(async (sessionId: string) => {
    if (useAppStore.getState().accessLevel === 'readonly') return;
    // WHY: the [label](note:<id>) link is stripped
    // authoritatively by the backend on DELETE /sessions/{id} (single source of
    // truth via the CRDT convergence path). No client-side remove-note-link emit.
    await deleteSession(sessionId);
    if (useNoteStore.getState().activeNoteThreadId === sessionId) {
      setActiveNoteThreadId(null);
    }
  }, [deleteSession, setActiveNoteThreadId]);

  const exitThread = useCallback(() => {
    // INVARIANT: leaving an empty note-chat thread (no messages) deletes the
    // session — both Esc inside NoteChatView and the "back to all notes"
    // button must follow this rule. Why: an anchorless note with zero
    // messages is the same as cancelling creation; persisting it leaks
    // empty cards into the list and the editor link map.
    // ARCH: link strip is backend-authoritative on
    // delete; no client emit here.
    const store = useNoteChatStore.getState();
    const sessionId = store.activeSessionId;
    if (sessionId && store.messages.length === 0) {
      void store.deleteSession(sessionId);
    }
    // INVARIANT: exit stays on the notes tab. previousRightTab is only restored
    // when non-null — set only by editor-initiated note creation
    // (markdown-actions.createNote). Why: anchorless notes are created from
    // the notes panel itself; restoring a null previousRightTab would close
    // the right panel and leave a blank surface under the tab bar.
    const previousTab = useNoteStore.getState().previousRightTab;
    setActiveNoteThreadId(null, currentDocument?.document_id);
    useNoteChatStore.getState().setActiveSession(null);
    if (previousTab) {
      useNoteStore.getState().setPreviousRightTab(null);
      const docId = currentDocument?.document_id ?? null;
      useUIStore.getState().setRightPanelTab(docId, previousTab);
    }
  }, [setActiveNoteThreadId, currentDocument?.document_id]);

  // WHY: the split-mode filter ignores document
  // content, so gate the content dependency — without this, doc typing (collab content
  // updates) would spuriously re-sort/re-filter the notes list and re-render every
  // card. In split the sentinel is null (stable); otherwise it tracks the content the
  // ref-mode/doc branches actually read.
  const noteItemsContentDep = isSplitMode ? null : currentDocument?.content;

  const noteItems = useMemo(() => {
    // M2: sort a COPY, not the store array the
    // selector returns — mutating it corrupts the store snapshot other selectors read.
    const items = [...noteSessions]
      .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
    // WHY: in split view both columns are visible,
    // so show ALL document notes (anchored + anchorless) plus the open reference's
    // notes. This branch runs BEFORE the ref-mode filter because isSplitMode implies
    // isRefMode (currentReference set) — the ref-mode filter would otherwise drop
    // anchored doc notes and keep only reference notes.
    if (isSplitMode && currentReference) {
      return selectSplitNoteItems(items, currentReference.reference_id);
    }
    if (isRefMode && currentReference) {
      const content = currentDocument?.content ?? '';
      const docAnchoredIds = new Set<string>();
      const re = /\(note:([^)\s]+)\)/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(content)) !== null) docAnchoredIds.add(m[1]);
      return items.filter(s =>
        s.reference_id === currentReference.reference_id
        || (s.reference_id === null && !docAnchoredIds.has(s.session_id)),
      );
    }
    return items;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- noteItemsContentDep replaces currentDocument?.content (split ignores it)
  }, [noteSessions, isSplitMode, isRefMode, currentReference?.reference_id, noteItemsContentDep]);

  return {
    isRefMode,
    isSplitMode,
    noteItems,
    sessionsLoading,
    activeNoteSessionId,
    handleDelete,
    exitThread,
  };
}
