/**
 * Notes panel backed by chat sessions API (is_note=true).
 * Shows note session list grouped by reference; clicking opens NoteChatView.
 * When a note thread is open, the panel acts as a drop zone for images.
 *
 * Custom events consumed: 'highlight-note', 'create-note-from-editor', 'document-deleted',
 *   'collab-note-created' (full session OR minimal {session_id} → list refresh),
 *   'collab-note-updated' (payload: {action: added|edited|deleted, session_id, ...}),
 *   'collab-note-deleted' (payload: {sessionId}).
 * Store slices: note-chat-store (sessions, messages), note-store (activeNoteThreadId).
 */

import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { ArrowLeft, Trash2, FileText, Plus, MessageSquare, Paperclip } from 'lucide-react';
import { EditorView } from '@codemirror/view';
import { DEFAULT_HOTKEYS } from './editor/hotkey-config';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { readRefOpenMode, refIsScope } from '../store/ui-store/documents-slice';
import { useNoteStore } from '../store/note-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { claimAutoOpen, openInboxObject } from '../store/inbox-store';
import { emit } from '../events';
import { getRoleView, type EditorRole } from '../editor/active-editor';
import { useArmedAction } from '../hooks/useArmedAction';
import { useImageDropHandlers } from '../hooks/useImageDropHandlers';
import { useRightPanelHoverPreview } from '../hooks/useRightPanelHoverPreview';
import { useReferencePreview } from '../hooks/useReferencePreview';
import { useTranslation } from '../i18n';
import { Button, IconButton, ListPill, PanelLoading, PillList } from './ui';
import { ListRow } from './chat/ListRow';
import { HoverPreviewPopup } from './HoverPreviewPopup';
import { NoteChatView } from './NoteChatView';
import { useNoteCrud } from '../hooks/useNoteCrud';
import { getEntityHandle } from '../collab/active-handle-registry';
import { resolveNoteAnchor } from '../collab/note-anchor-wire';
import { findNoteAnchorRange } from '../editor/note-highlight';
import { noteOwnerId } from './editor/note-anchor-wiring';
import { referenceFileUrl } from '../utils/reference-url';
import { formatDate } from '../utils/format';
import { ChatSession, Reference } from '../types';

interface NoteGroup {
  referenceId: string | null;
  title: string;
  notes: ChatSession[];
}

function useNoteGroups(noteItems: ChatSession[], references: Reference[], isRefMode: boolean) {
  return useMemo(() => {
    if (isRefMode || noteItems.length === 0) {
      return [{ referenceId: null, title: '', notes: noteItems }] as NoteGroup[];
    }

    const refMap = new Map<string, string>();
    for (const ref of references) {
      refMap.set(ref.reference_id, ref.title);
    }

    const docNotes: ChatSession[] = [];
    const refGroups = new Map<string, ChatSession[]>();

    for (const session of noteItems) {
      if (session.reference_id) {
        let arr = refGroups.get(session.reference_id);
        if (!arr) {
          arr = [];
          refGroups.set(session.reference_id, arr);
        }
        arr.push(session);
      } else {
        docNotes.push(session);
      }
    }

    const groups: NoteGroup[] = [];
    if (docNotes.length > 0) {
      groups.push({ referenceId: null, title: '', notes: docNotes });
    }
    for (const [refId, notes] of refGroups) {
      groups.push({
        referenceId: refId,
        title: refMap.get(refId) || refId,
        notes,
      });
    }
    return groups;
  }, [noteItems, references, isRefMode]);
}

/** How a note card shows its anchor: on a fragment, on a deleted fragment, or on none. */
type CardAnchor = 'anchored' | 'orphan' | 'none';

function NoteSessionCard({
  session,
  anchor,
  isHighlighted,
  onClick,
  onDelete,
  onHover,
  onHoverLeave,
  materialIcon,
}: {
  session: ChatSession;
  anchor: CardAnchor;
  isHighlighted: boolean;
  onClick: () => void;
  onDelete: () => void;
  onHover: (e: React.MouseEvent) => void;
  onHoverLeave: (e: React.MouseEvent) => void;
  /** Optional material-ownership marker (split view only): FileText = document
   *  note, Paperclip = reference note. Composed into the meta row before the
   *  discussion icon so a split user can tell which column a note belongs to. */
  materialIcon?: React.ReactNode;
}) {
  const deleteAction = useArmedAction();
  const accessLevel = useAppStore(s => s.accessLevel);
  const canDelete = accessLevel === 'full' || accessLevel === 'commentator';
  const { t } = useTranslation();

  const variant = anchor !== 'anchored'
    ? 'anchorless' as const
    : isHighlighted
      ? 'highlight' as const
      : 'anchored' as const;

  // WHY: the pill layout mirrors
  // the chat list row (ChatRow) — a single-line truncated title (first message text,
  // chat-title size) + a dim meta row carrying the discussion icon + last-message
  // date when the note has a discussion (>1 message). The hover preview popup (last
  // message) is wired at the panel level via the shared LinkPreviewPopup, identical
  // in format/positioning to the references hover preview.
  const count = session.message_count ?? 0;
  const hasDiscussion = count > 1;
  const body = session.first_message_preview?.trim() || t('untitledNote');
  // WHY: a single-message note still has a date worth showing. The discussion icon stays
  // gated on hasDiscussion so icon ⇒ discussion is preserved while every card shows a date.
  const date = formatDate(
    session.last_message_at || session.created_at || session.updated_at,
  );
  // An orphan stays listed (never auto-deleted, never re-anchored) and says why it is grey.
  // WHY: a system note's title (widget key label + caller title, pipeline
  // errors) is documented behavior (configs/help/06-notes.md: the widget note
  // "is titled with the key's name") and rides the wire unused — surface it as
  // the meta row's leading term. Member notes carry title "" and stay
  // date-only. The title is caller-supplied and unbounded, so it rides the SAME
  // single-line truncated meta span as the date, never a wrapping node.
  const noteTitle = session.title?.trim();
  const meta = [
    ...(noteTitle ? [noteTitle] : []),
    ...(anchor === 'orphan' ? [t('noteFragmentDeleted')] : []),
    date,
  ].join(' · ');

  return (
    <ListPill
      variant={variant}
      unread={session.unread === true}
      id={`note-${session.session_id}`}
      onClick={onClick}
      onMouseEnter={onHover}
      onMouseLeave={onHoverLeave}
    >
      <ListRow
        body={body}
        bodyLines={2}
        meta={meta}
        // WHY: in split mode prepend the material
        // marker before the discussion icon, both inside the shared metaIcon slot so
        // the existing icon+date rhythm is preserved. metaIcon wraps both in a flex
        // span when a material marker is present.
        metaIcon={materialIcon != null
          ? <span className="flex items-center gap-1">{materialIcon}{hasDiscussion ? <MessageSquare size={12} /> : null}</span>
          : (hasDiscussion ? <MessageSquare size={12} /> : null)}
      />
      {canDelete && (
        <IconButton
          size="sm"
          danger
          filled={deleteAction.armed}
          // WHY: delete button floats at the top-right of the card, on the same
          // level as the title, overlapping the text only when revealed on hover.
          className={`absolute top-2 right-2 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 transition-opacity note-delete-btn${deleteAction.armed ? ' note-delete-armed' : ''}`}
          title={t('deleteNote')}
          onClick={(e: React.MouseEvent) => {
            e.stopPropagation();
            deleteAction.handleClick(onDelete);
          }}
          onMouseLeave={deleteAction.disarm}
        >
          <Trash2 size={13} />
        </IconButton>
      )}
    </ListPill>
  );
}

import type React from 'react';

export function NotesPanel() {
  const accessLevel = useAppStore(s => s.accessLevel);
  const currentProject = useAppStore(s => s.currentProject);
  const currentDocument = useAppStore(s => s.currentDocument);
  const rawReference = useAppStore(s => s.currentReference);
  const references = useAppStore(s => s.references);
  // Panel quick preview: the notes panel is the DOCUMENT's (decision A). Same
  // projection as useNoteCrud, so isRefMode/isSplitMode and the anchored-scan /
  // click / create paths below all agree on one scope.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocument?.document_id ?? ''], s.compactLayout));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  const activeNoteThreadId = useNoteStore(s => s.activeNoteThreadId);
  const addPendingImage = useNoteChatStore(s => s.addPendingImage);
  const setActiveNoteThreadId = useNoteStore(s => s.setActiveNoteThreadId);
  const canComment = accessLevel !== 'readonly';
  const canDelete = accessLevel === 'full' || accessLevel === 'commentator';
  const headerDelete = useArmedAction();
  const { t } = useTranslation();

  const [highlightedNoteId, setHighlightedNoteId] = useState<string | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const highlightNote = useCallback((noteId: string) => {
    if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current);
    setHighlightedNoteId(noteId);
    setTimeout(() => {
      const el = document.getElementById(`note-${noteId}`);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 100);
    highlightTimerRef.current = setTimeout(() => setHighlightedNoteId(null), 3000);
  }, []);

  const crud = useNoteCrud({ highlightNote });
  const isRefMode = crud.isRefMode;
  const isSplitMode = crud.isSplitMode;
  const isThreadView = !!activeNoteThreadId && !!crud.activeNoteSessionId;

  const noteGroups = useNoteGroups(crud.noteItems, references, isRefMode);

  // INVARIANT: a card's anchor state comes from the note's anchor resolved against its
  // OWNING material's live ydoc — the same resolution the editor highlight paints from.
  // Why: card colour, editor highlight and connector must agree on one source; a
  // pending anchor (its Yjs update not here yet) shows as anchored, never as an orphan.
  const cardAnchors = useMemo(() => {
    const out = new Map<string, CardAnchor>();
    for (const s of crud.noteItems) {
      if (!s.anchor_rel_start || !s.anchor_rel_end) { out.set(s.session_id, 'none'); continue; }
      const owner = noteOwnerId(s);
      const ydoc = owner ? getEntityHandle(owner)?.ydoc : undefined;
      // Owning material not open here: the anchor cannot be judged, so it is not called lost.
      if (!ydoc) { out.set(s.session_id, 'anchored'); continue; }
      const res = resolveNoteAnchor(s.anchor_rel_start, s.anchor_rel_end, ydoc);
      out.set(s.session_id, res === null ? 'none' : res.state === 'orphan' ? 'orphan' : 'anchored');
    }
    return out;
  }, [crud.noteItems]);

  // WHY: shared image-drop capture (useImageDropHandlers) — same policy + budget
  // helpers as ChatPanel, so the chat and note drop paths cannot drift. The drop is
  // always swallowed (preventDefault+stopPropagation) so it never reaches the
  // references path; images enqueue only when a note thread is open (enabled).
  const { isDragging, dragHandlers } = useImageDropHandlers({
    getPending: () => useNoteChatStore.getState().pendingImages,
    addImage: addPendingImage,
    enabled: isThreadView,
  });

  // Hover preview — shared right-panel positioning hook (identical format + position
  // to the references hover). Two mutually exclusive targets share the single popup:
  //  - reference group header (hoverRefId): shows the reference's content/image,
  //    mirroring ReferencesPanel (imageUrl via the shared referenceFileUrl helper).
  //  - note pill (hoverNoteId): shows the note's LAST message preview.
  // Entering one target clears the other's id, so the memo resolves to exactly one
  // source (hoverRefId takes priority as a safety default).
  const hover = useRightPanelHoverPreview();
  const [hoverNoteId, setHoverNoteId] = useState<string | null>(null);
  const [hoverRefId, setHoverRefId] = useState<string | null>(null);
  // Lazy-fetch the hovered reference's body (the list is metadata-only). Image refs
  // resolve from file_path; already-hydrated text refs use their store content.
  // File refs never fetch — a downloadable binary has no body.
  const hoverRefFetchId = (() => {
    const ref = hoverRefId ? references.find(r => r.reference_id === hoverRefId) : null;
    return ref && ref.media_type !== 'image' && ref.media_type !== 'file' && ref.content === undefined ? hoverRefId : null;
  })();
  const { preview: hoverRefFetched, error: hoverRefError, loading: hoverRefLoading } = useReferencePreview(hoverRefFetchId);
  const { hoverTitle, hoverContent, hoverImageUrl } = useMemo(() => {
    if (hoverRefId) {
      const ref = references.find(r => r.reference_id === hoverRefId);
      if (!ref) return { hoverTitle: undefined, hoverContent: '', hoverImageUrl: undefined };
      if (ref.media_type === 'image' && ref.file_path)
        return { hoverTitle: ref.title, hoverContent: '', hoverImageUrl: referenceFileUrl(ref.reference_id, ref.file_path) };
      if (ref.content !== undefined) return { hoverTitle: ref.title, hoverContent: ref.content, hoverImageUrl: undefined };
      return { hoverTitle: ref.title, hoverContent: hoverRefFetched?.content ?? '', hoverImageUrl: undefined };
    }
    if (hoverNoteId) {
      const s = crud.noteItems.find(n => n.session_id === hoverNoteId);
      // WHY no title: a note has none — its first message is already the pill's text, so
      // a plaque would only repeat it; the popup shows the last message alone.
      return { hoverTitle: undefined, hoverContent: s?.last_message_preview?.trim() ?? '', hoverImageUrl: undefined };
    }
    return { hoverTitle: undefined, hoverContent: '', hoverImageUrl: undefined };
  }, [hoverRefId, hoverNoteId, references, crud.noteItems, hoverRefFetched]);
  const handleNoteHover = useCallback((sessionId: string, el: HTMLElement) => {
    setHoverRefId(null);
    setHoverNoteId(sessionId);
    hover.handleHover(el);
  }, [hover]);
  const handleRefHeaderHover = useCallback((refId: string, el: HTMLElement) => {
    setHoverNoteId(null);
    setHoverRefId(refId);
    hover.handleHover(el);
  }, [hover]);
  const handleHoverLeaveAll = useCallback((e?: React.MouseEvent) => {
    setHoverRefId(null);
    setHoverNoteId(null);
    hover.handleHoverLeave(e);
  }, [hover]);
  // INVARIANT(no-silent-degradation): the empty-body gate must not swallow the ref fetch's
  // loading/error states. Why: `hoverContent` is '' while a ref body is in flight AND when
  // it failed, so gating on `!== ''` alone hid the popup in exactly the two cases the popup
  // now exists to report. A hovered NOTE with no preview still stays hidden (its branch
  // never fetches, so loading/error are false there).
  const hoverVisible = hover.visible
    && (hoverContent !== '' || !!hoverImageUrl || hoverRefLoading || hoverRefError);


  const handleNoteClick = useCallback((session: ChatSession) => {
    // see SYSTEM: inbox — read = OPENED: any path that opens the note (thread
    // open here, or the pendingNoteNavigation that opens it after the reference
    // jump) clears the viewer's flag. Fire-and-forget: openInboxObject reports
    // its own failure; the ws:inbox_changed refetch reconciles the store.
    if (session.unread) {
      void openInboxObject(currentDocument?.document_id, 'note', session.session_id);
    }
    // WHY: in split view focus the OWNING column
    // (programmatic view.focus() runs claimFocus so caret/hotkeys/
    // snapshot target the owning material), open the thread, and scroll THAT
    // column to the anchor inline. The shared scroll-to-note-in-editor event
    // stays primary-only (useEditorEvents disabled on secondary) so write/navigate
    // paths are NOT enabled on the secondary editor.
    if (isSplitMode && currentReference) {
      const owningRole: EditorRole =
        session.reference_id === currentReference.reference_id ? 'secondary' : 'primary';
      const view = getRoleView(owningRole);
      view?.focus();
      useNoteStore.getState().setActiveNoteThreadId(session.session_id, currentDocument?.document_id);
      useNoteChatStore.getState().setActiveSession(session.session_id);
      useNoteChatStore.getState().setPendingInputFocus(true);
      if (view) {
        const pos = findNoteAnchorRange(view.state, session.session_id)?.from;
        if (pos != null) view.dispatch({ effects: EditorView.scrollIntoView(pos, { y: 'center' }) });
      }
      if (session.anchor_rel_start != null) {
        useNoteStore.getState().setConnectedNoteId(session.session_id);
      }
      return;
    }
    if (session.reference_id && !isRefMode) {
      useNoteStore.getState().setPendingNoteNavigation({ noteId: session.session_id });
      emit('navigate-to-reference', { referenceId: session.reference_id });
      return;
    }
    useNoteStore.getState().setActiveNoteThreadId(session.session_id, currentDocument?.document_id);
    useNoteChatStore.getState().setActiveSession(session.session_id);
    useNoteChatStore.getState().setPendingInputFocus(true);
    emit('scroll-to-note-in-editor', { noteId: session.session_id });
    if (session.anchor_rel_start != null) {
      useNoteStore.getState().setConnectedNoteId(session.session_id);
    }
  }, [isSplitMode, isRefMode, currentReference?.reference_id, currentDocument?.document_id]);

  // see SYSTEM: inbox — opening the notes tab while flagged notes exist opens
  // the EARLIEST flagged one (created_at ASC). Waits for the list to settle
  // (loading gate + sessionsScope identity: doc-switch commits render before
  // DocumentPage's loadSessions drops the previous doc's rows — without the
  // scope check the effect would open the OLD doc's note under the new one),
  // skips a thread already open (no intrusion into an open read) and fires
  // once per arrival (claimAutoOpen, see its INVARIANT in inbox-store).
  const sessionsScope = useNoteChatStore(s => s.sessionsScope);
  useEffect(() => {
    const projectId = currentProject?.project_id;
    const docId = currentDocument?.document_id;
    const expectedScope = projectId && docId
      ? `${projectId}:${docId}:note`
      : null;
    if (
      !docId || isThreadView || crud.sessionsLoading
      || sessionsScope !== expectedScope
    ) return;
    const earliest = crud.noteItems
      .filter(s => s.unread === true)
      .sort((a, b) => (a.created_at ?? '').localeCompare(b.created_at ?? ''))[0];
    if (!earliest) return;
    if (!claimAutoOpen('note', docId)) return;
    handleNoteClick(earliest);
  }, [currentProject?.project_id, currentDocument?.document_id, isThreadView, crud.sessionsLoading, crud.noteItems, sessionsScope, handleNoteClick]);

  const handleCreateNote = useCallback(async () => {
    if (!currentProject || !currentDocument) return;
    try {
      const session = await useNoteChatStore.getState().createNoteSession({
        projectId: currentProject.project_id,
        documentId: currentDocument.document_id,
        // BUG FIX: an anchorless note created from the Notes panel attaches to the
        // OPEN reference when one is present, not to the parent document.
        referenceId: currentReference?.reference_id,
      });
      setActiveNoteThreadId(session.session_id, currentDocument.document_id);
      useNoteChatStore.getState().setPendingInputFocus(true);
    } catch {
      useAppStore.getState().showToast(t('failedToCreateNote'), 'error');
    }
  }, [currentProject, currentDocument, currentReference?.reference_id, setActiveNoteThreadId, t]);

  return (
    <div
      className="flex flex-col h-full relative"
      {...dragHandlers}
    >
      {isThreadView ? (
        <div className="flex items-center justify-between gap-1 px-3 py-2 border-b border-[var(--warm-sep)] bg-[var(--warm-tint)] min-h-[40px]">
          <Button variant="ghost-note" size="sm" onClick={crud.exitThread}>
            <ArrowLeft size={13} />
            {t('backToAllNotes')}
          </Button>
          {canDelete && (
            <IconButton
              size="sm"
              danger
              theme="note"
              filled={headerDelete.armed}
              title={t('deleteNote')}
              onClick={() => headerDelete.handleClick(() => crud.handleDelete(activeNoteThreadId!))}
              onMouseLeave={headerDelete.disarm}
            >
              <Trash2 size={13} />
            </IconButton>
          )}
        </div>
      ) : canComment ? (
        <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
          <Button variant="ghost" size="sm" onClick={handleCreateNote}>
            <Plus size={13} />
            {t('addNote')}
          </Button>
        </div>
      ) : null}

      {isThreadView ? (
        <div className="flex-1 min-h-0 flex flex-col overflow-y-auto">
          <NoteChatView />
        </div>
      ) : (
        <PillList className="min-h-0">
          {/* INVARIANT: spinner (not empty state) while the note list is loading
              with nothing yet to show — empty state ≠ loading state.  Why: while the note list loads with nothing yet shown, a spinner (not the empty state) keeps loading distinct from empty. */}
          {crud.sessionsLoading && crud.noteItems.length === 0 ? (
            <PanelLoading />
          ) : crud.noteItems.length === 0 ? (
            <div className="py-4 px-2 text-ui-base text-text-dim text-center">
              {!canComment ? t('noNotes') : (() => {
                const noteKey = Object.entries(DEFAULT_HOTKEYS).find(([, action]) => action === 'createNote')?.[0];
                const formatKey = (key: string) => {
                  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
                  return key
                    .replace('Mod-', isMac ? '⌘' : 'Ctrl+')
                    .replace('Shift-', isMac ? '⇧' : 'Shift+')
                    .replace('Alt-', isMac ? '⌥' : 'Alt+')
                    .replace(/-/g, '')
                    .replace(/([a-z])$/, (_, c) => c.toUpperCase());
                };
                return <>
                  {t('noNotesYetHint')}
                  {noteKey && <><br />{t('orSelectTextHint', { hotkey: formatKey(noteKey) })}</>}
                </>;
              })()}
            </div>
          ) : (
            noteGroups.map(group => (
              <div key={group.referenceId ?? '__doc__'} className="flex flex-col gap-[var(--pill-gap)]">
                {group.referenceId && !isRefMode && (
                  <div
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-surface2/60 text-ui-sm text-accent-text cursor-pointer border-b border-border"
                    onClick={() => emit('navigate-to-reference', { referenceId: group.referenceId! })}
                    onMouseEnter={(e) => handleRefHeaderHover(group.referenceId!, e.currentTarget)}
                    onMouseLeave={handleHoverLeaveAll}
                  >
                    <FileText size={12} />
                    <span className="truncate">{group.title}</span>
                  </div>
                )}
                {group.notes.map(session => (
                  <NoteSessionCard
                    key={session.session_id}
                    session={session}
                    // WHY: the yellow "anchored" color is driven ONLY by a text
                    // anchor, never by reference attachment — an anchorless note
                    // attached to a reference must stay gray — reference_id is
                    // orthogonal to color; it is still used for navigation.
                    anchor={cardAnchors.get(session.session_id) ?? 'none'}
                    isHighlighted={highlightedNoteId === session.session_id}
                    // WHY: material marker shown only
                    // in split view, where the merged list mixes doc + reference notes.
                    materialIcon={isSplitMode && currentReference
                      ? (session.reference_id === currentReference.reference_id
                        ? <span title={t('noteBelongsToReference')}><Paperclip size={12} /></span>
                        : <span title={t('noteBelongsToDocument')}><FileText size={12} /></span>)
                      : undefined}
                    onClick={() => handleNoteClick(session)}
                    onDelete={() => crud.handleDelete(session.session_id)}
                    onHover={(e) => handleNoteHover(session.session_id, e.currentTarget as HTMLElement)}
                    onHoverLeave={handleHoverLeaveAll}
                  />
                ))}
              </div>
            ))
          )}
        </PillList>
      )}
      {isDragging && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-bg/60 border-2 border-dashed border-accent pointer-events-none">
          <span className="text-text-muted text-sm">{t('dropImagesHere')}</span>
        </div>
      )}
      <HoverPreviewPopup
        hover={hover}
        visible={hoverVisible}
        title={hoverTitle}
        content={hoverContent}
        imageUrl={hoverImageUrl}
        error={hoverRefError}
        loading={hoverRefLoading}
      />
    </div>
  );
}
