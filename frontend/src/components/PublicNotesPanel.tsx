/**
 * PublicNotesPanel — the notes tab of a public link with comments on.
 *
 * Lists every note on the shared document (author names, no ids), opens a thread,
 * and lets a guest start a note (from a selection via the toolbar / Mod-m, or on the
 * whole document from here), reply, and delete their own words. A new note exists
 * only once its first message is sent — the public wire creates both at once.
 *
 * Store: public-notes-store (list, open thread, draft, guest name). Events consumed:
 * none — PublicSharePage routes 'open-notes' into the store. The member panel
 * (NotesPanel) is not reused: see the ARCH note in public-notes-store.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import { ArrowLeft, Plus, Trash2 } from 'lucide-react';
import { EditorView } from '@codemirror/view';
import {
  usePublicNotesStore, usePublicCanNote, AUTHOR_NAME_MAX, PUBLIC_NOTE_CONTENT_MAX,
} from '../store/public-notes-store';
import { useAppStore } from '../store/app-store';
import { getEditorView } from '../editor/active-editor';
import { findNoteAnchorRange } from '../editor/note-highlight';
import { useArmedAction } from '../hooks/useArmedAction';
import { useTranslation } from '../i18n';
import { formatDate } from '../utils/format';
import { Button, FieldInput, IconButton, ListPill, PanelLoading, PillList } from './ui';
import { ChatComposer } from './chat/ChatComposer';
import { ListRow } from './chat/ListRow';
import { MessageBubble, type MessageBubbleActions } from './chat/MessageBubble';
import { copyWithToast } from './chat/shared/copy';
import type { PublicNote, PublicNoteMessage } from '../api/public-share';
import type { ChatMessage } from '../types';

function toChatMessage(m: PublicNoteMessage, sessionId: string): ChatMessage {
  return {
    message_id: m.message_id,
    chat_id: sessionId,
    parent_id: m.parent_id,
    role: 'user',
    content: m.content,
    author_name: m.author_name ?? undefined,
    created_at: m.created_at ?? '',
  };
}

/** Scroll the public editor to a note's painted fragment (nothing when it is not painted). */
function revealNote(sessionId: string): void {
  const view = getEditorView();
  const range = view ? findNoteAnchorRange(view.state, sessionId) : null;
  if (view && range) view.dispatch({ effects: EditorView.scrollIntoView(range.from, { y: 'center' }) });
}

function NoteCard({ note, onOpen, onDelete }: { note: PublicNote; onOpen: () => void; onDelete: () => void }) {
  const { t } = useTranslation();
  const deleteAction = useArmedAction();
  // A guest deletes a note from the list only while it is their single message; a
  // note with a discussion is deleted from its thread (the backend's own-words rule).
  const canDelete = note.is_mine && note.messages.length === 1 && note.messages[0].is_mine;
  const first = note.messages[0];
  const last = note.messages[note.messages.length - 1];
  const orphan = note.anchor?.state === 'orphan';
  const variant = note.anchor && !orphan ? 'anchored' as const : 'anchorless' as const;
  const date = formatDate(last?.created_at ?? note.created_at ?? '');
  const author = first?.author_name ?? t('unknownAuthor');
  const meta = orphan ? `${t('noteFragmentDeleted')} · ${author} · ${date}` : `${author} · ${date}`;
  return (
    <ListPill variant={variant} id={`note-${note.session_id}`} onClick={onOpen}>
      <ListRow body={first?.content.trim() || t('untitledNote')} bodyLines={2} meta={meta} />
      {canDelete && (
        <IconButton
          size="sm"
          danger
          filled={deleteAction.armed}
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

/**
 * The guest's composer — the members' ChatComposer (note variant, so the same warm
 * chrome), with the guest's name above the text and no mic (no transcription
 * endpoint is reachable anonymously).
 */
function Composer() {
  const { t } = useTranslation();
  const authorName = usePublicNotesStore(s => s.authorName);
  const setAuthorName = usePublicNotesStore(s => s.setAuthorName);
  const submit = usePublicNotesStore(s => s.submit);
  const sending = usePublicNotesStore(s => s.sending);
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const nameMissing = authorName.trim().length === 0;
  const canSend = !sending && !nameMissing && text.trim().length > 0;

  // Opening a thread (or starting a note) puts the caret in the composer, as for members.
  useEffect(() => { textareaRef.current?.focus(); }, []);

  const send = useCallback(async () => {
    if (!canSend) return;
    if (await submit(text)) setText('');
  }, [canSend, submit, text]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      void send();
    }
  };

  return (
    <ChatComposer
      variant="note"
      value={text}
      onChange={e => setText(e.target.value)}
      onSend={() => { void send(); }}
      onKeyDown={onKeyDown}
      isStreaming={false}
      canSend={canSend}
      placeholder={t('noteChatPlaceholder')}
      maxLength={PUBLIC_NOTE_CONTENT_MAX}
      images={NO_IMAGES}
      onRemoveImage={noop}
      textareaRef={textareaRef}
      topControls={(
        <div className="flex flex-col gap-1 pb-1">
          <FieldInput
            value={authorName}
            onChange={e => setAuthorName(e.target.value)}
            placeholder={t('publicNoteNamePlaceholder')}
            maxLength={AUTHOR_NAME_MAX}
            autoComplete="nickname"
          />
          {nameMissing && <p className="text-ui-xs text-text-dim">{t('publicNoteNameRequired')}</p>}
        </div>
      )}
    />
  );
}

const NO_IMAGES: string[] = [];
const noop = () => {};

function ThreadView({ note }: { note: PublicNote | null }) {
  const deleteMessage = usePublicNotesStore(s => s.deleteMessage);
  const closeThread = usePublicNotesStore(s => s.closeThread);
  const showToast = useAppStore(s => s.showToast);
  const parentIds = useMemo(
    () => new Set((note?.messages ?? []).map(m => m.parent_id).filter((v): v is string => !!v)),
    [note],
  );
  const actions = useMemo<MessageBubbleActions>(() => ({
    onCopy: (content: string) => copyWithToast(content, showToast),
    onDelete: (messageId: string) => { if (note) void deleteMessage(note.session_id, messageId); },
  }), [note, deleteMessage, showToast]);

  // WHY: Escape lives on the thread, not the textarea, so it fires from any focused
  // control — a guest without a name has the caret in the name field. Leaving drops an
  // unsent draft: a new note exists only once its first message is sent.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key !== 'Escape' || e.defaultPrevented || !e.currentTarget.contains(e.target as Node)) return;
    e.preventDefault();
    closeThread();
  };

  return (
    <div className="flex flex-col flex-1 min-h-0" onKeyDown={onKeyDown}>
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {note?.messages.map(m => (
          <MessageBubble
            key={m.message_id}
            message={toChatMessage(m, note.session_id)}
            variant="note"
            isOwn={m.is_mine}
            hasChildren={parentIds.has(m.message_id)}
            actions={actions}
          />
        ))}
      </div>
      <Composer />
    </div>
  );
}

export function PublicNotesPanel() {
  const { t } = useTranslation();
  const notes = usePublicNotesStore(s => s.notes);
  // WHY: the page refetches the list on every change-ping, so `loading` flips on each
  // one — only the first load (no list yet) shows the loader, or an empty list flashes it.
  const firstLoad = usePublicNotesStore(s => s.loading && !s.loaded);
  const notesSha = usePublicNotesStore(s => s.notesSha);
  const servedSha = usePublicNotesStore(s => s.servedSha);
  const refreshFailed = usePublicNotesStore(s => s.refreshFailed);
  const liveLost = usePublicNotesStore(s => s.liveLost);
  const activeNoteId = usePublicNotesStore(s => s.activeNoteId);
  const draft = usePublicNotesStore(s => s.draft);
  const draftSeq = usePublicNotesStore(s => s.draftSeq);
  const openNote = usePublicNotesStore(s => s.openNote);
  const closeThread = usePublicNotesStore(s => s.closeThread);
  const startNote = usePublicNotesStore(s => s.startNote);
  const deleteNote = usePublicNotesStore(s => s.deleteNote);
  const canNote = usePublicCanNote();
  const headerDelete = useArmedAction();

  const activeNote = activeNoteId ? notes.find(n => n.session_id === activeNoteId) ?? null : null;
  const isThread = !!draft || !!activeNote;
  // Whole-note delete is the guest's only while every word in it is theirs (the
  // backend refuses otherwise; this hides the trigger).
  const canDeleteNote = !!activeNote && activeNote.is_mine && activeNote.messages.every(m => m.is_mine);
  // The listed anchors were resolved against newer text than the editor shows.
  const stale = !!notesSha && !!servedSha && notesSha !== servedSha;
  const ordered = useMemo(() => [...notes].reverse(), [notes]);

  const open = useCallback((note: PublicNote) => {
    openNote(note.session_id);
    revealNote(note.session_id);
  }, [openNote]);

  return (
    <div className="flex flex-col h-full relative">
      {isThread ? (
        <div className="flex items-center justify-between gap-1 px-3 py-2 border-b border-[var(--warm-sep)] bg-[var(--warm-tint)] min-h-[40px]">
          <Button variant="ghost-note" size="sm" onClick={closeThread}>
            <ArrowLeft size={13} />
            {t('backToAllNotes')}
          </Button>
          {canDeleteNote && (
            <IconButton
              size="sm"
              danger
              theme="note"
              filled={headerDelete.armed}
              title={t('deleteNote')}
              onClick={() => headerDelete.handleClick(() => { void deleteNote(activeNote.session_id); })}
              onMouseLeave={headerDelete.disarm}
            >
              <Trash2 size={13} />
            </IconButton>
          )}
        </div>
      ) : canNote ? (
        <div className="flex items-center gap-1 px-3 py-2 border-b border-border bg-surface min-h-[40px]">
          <Button variant="ghost" size="sm" onClick={() => startNote({ selection: null, quote: '' })}>
            <Plus size={13} />
            {t('addNote')}
          </Button>
        </div>
      ) : null}

      {isThread ? (
        // WHY keyed: switching from one thread to another (or to a new note) remounts the
        // composer, so its text is the new thread's and the caret lands in it.
        <ThreadView key={draft ? `draft-${draftSeq}` : activeNoteId ?? ''} note={activeNote} />
      ) : (
        <div className="flex-1 min-h-0 flex flex-col">
          {refreshFailed && <p className="px-3 py-2 text-ui-xs text-text-dim border-b border-border">{t('publicNotesRefreshFailed')}</p>}
          {liveLost && <p className="px-3 py-2 text-ui-xs text-text-dim border-b border-border">{t('publicNotesLiveLost')}</p>}
          {stale && <p className="px-3 py-2 text-ui-xs text-text-dim border-b border-border">{t('publicNotesStale')}</p>}
          <PillList className="min-h-0">
            {firstLoad ? (
              <PanelLoading />
            ) : notes.length === 0 ? (
              <div className="py-4 px-2 text-ui-base text-text-dim text-center">{t('publicNotesEmpty')}</div>
            ) : (
              ordered.map(note => <NoteCard key={note.session_id} note={note} onOpen={() => open(note)} onDelete={() => { void deleteNote(note.session_id); }} />)
            )}
          </PillList>
        </div>
      )}
    </div>
  );
}
