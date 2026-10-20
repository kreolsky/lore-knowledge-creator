/**
 * Public notes store — comments a guest leaves on a public link (routes/public_notes.py).
 *
 * ARCH: a store of its own, not a branch of note-chat-store. The public wire differs in
 * every verb the member store owns: a note is created TOGETHER with its first message
 * (there is no empty thread to open first), authorship is `author_name` + `is_mine`
 * instead of user ids, anchors arrive resolved to offsets instead of Yjs positions,
 * there is no edit, no image, no realtime frame and no inbox. The member store's
 * fresh-note / discard / SWR / logout-epoch machinery has nothing to act on here.
 *
 * The list is server truth, refetched on open, on tab focus and after every own write
 * (the page holds no socket); nothing here is optimistic.
 */
import { create } from 'zustand';
import {
  publicNotesByDoc, publicDocumentByDoc, createPublicNote, replyPublicNote,
  deletePublicNoteMessage, deletePublicNote, type PublicNote, type PublicNoteSelection,
} from '../api/public-share';
import { HttpError } from '../api/client';
import { useAppStore } from './app-store';
import { t } from '../i18n';
import { differingSpan, mapRangeThrough } from '../utils/differing-span';

const NAME_KEY = 'lore.anonName';
/** The highlight/connector id of the note being composed (it has no session id yet). */
export const PUBLIC_DRAFT_NOTE_ID = '__public-draft__';
export const AUTHOR_NAME_MAX = 40;
/** The server's cap on one guest message (routes/public_notes.py CONTENT_MAX). */
export const PUBLIC_NOTE_CONTENT_MAX = 2000;

function readStoredName(): string {
  try { return localStorage.getItem(NAME_KEY) ?? ''; } catch { return ''; }
}

/** A new note being composed: it exists on the server only once its first message is sent. */
export interface PublicNoteDraft {
  selection: PublicNoteSelection | null;
  quote: string;
}

interface PublicNotesState {
  documentId: string | null;
  allowComments: boolean;
  notes: PublicNote[];
  /** content_sha256 of the text the listed anchors were resolved against. */
  notesSha: string | null;
  /** content_sha256 of the text the page's editor shows (from the document payload). */
  servedSha: string | null;
  loading: boolean;
  /** A list for this document arrived at least once. */
  loaded: boolean;
  /**
   * The last refetch of an already-loaded list failed: the shown list is the previous
   * one and the panel says so (it is kept so an open thread and its composer survive).
   */
  refreshFailed: boolean;
  activeNoteId: string | null;
  draft: PublicNoteDraft | null;
  /**
   * Bumped by every startNote, never by refreshText: a new note is a fresh composer
   * (text cleared, caret in it), while a draft carried across a member's edit is not.
   */
  draftSeq: number;
  authorName: string;
  sending: boolean;

  setServed: (documentId: string, sha: string) => void;
  load: (documentId: string) => Promise<void>;
  /**
   * Re-read the document text in place (a member edited it) and carry the draft's
   * selection across the edit. False when nothing was applied.
   */
  refreshText: (documentId: string) => Promise<boolean>;
  /** Start composing a new note — over a selection, or on the whole document. */
  startNote: (draft: PublicNoteDraft) => void;
  openNote: (sessionId: string) => void;
  closeThread: () => void;
  setAuthorName: (name: string) => void;
  /** Send the composer: a new note for a draft, else a reply in the open note. */
  submit: (content: string) => Promise<boolean>;
  deleteMessage: (sessionId: string, messageId: string) => Promise<void>;
  deleteNote: (sessionId: string) => Promise<void>;
  reset: () => void;
}

const INITIAL = {
  documentId: null,
  allowComments: false,
  notes: [],
  notesSha: null,
  servedSha: null,
  loading: false,
  loaded: false,
  refreshFailed: false,
  activeNoteId: null,
  draft: null,
  draftSeq: 0,
  sending: false,
};

function toastError(key: Parameters<typeof t>[0]): void {
  useAppStore.getState().showToast(t(key), 'error');
}

export const usePublicNotesStore = create<PublicNotesState>((set, get) => ({
  ...INITIAL,
  authorName: readStoredName(),

  setServed(documentId, sha) {
    if (get().documentId !== documentId) {
      set({ ...INITIAL, documentId, servedSha: sha, authorName: get().authorName });
      return;
    }
    set({ servedSha: sha });
  },

  async load(documentId) {
    if (get().documentId !== documentId) {
      set({ ...INITIAL, documentId, authorName: get().authorName });
    }
    set({ loading: true });
    try {
      const data = await publicNotesByDoc(documentId);
      if (get().documentId !== documentId) return;
      set({
        allowComments: data.allow_comments, notes: data.notes, notesSha: data.content_sha256,
        loaded: true, refreshFailed: false,
      });
    } catch {
      if (get().documentId !== documentId) return;
      if (get().loaded) {
        // A failed REFETCH keeps the previous list, marked stale by the panel, and
        // toasts once per failure streak — the page polls, so a toast per attempt
        // would repeat every few seconds, and dropping the list would unmount an open
        // thread with the guest's unsent reply.
        if (!get().refreshFailed) toastError('failedToLoadNotes');
        set({ refreshFailed: true });
        return;
      }
      // No silent degradation: the first list never arrived — nothing is shown as current.
      set({ notes: [], notesSha: null });
      toastError('failedToLoadNotes');
    } finally {
      if (get().documentId === documentId) set({ loading: false });
    }
  },

  async refreshText(documentId) {
    let resp;
    try {
      resp = await publicDocumentByDoc(documentId);
    } catch {
      // WHY no toast: the panel already says the document changed; the next poll
      // retries, and nothing stale paints — the hashes still differ.
      return false;
    }
    const app = useAppStore.getState();
    const cur = app.currentDocument;
    if (cur?.document_id !== documentId || get().documentId !== documentId) return false;
    // INVARIANT: a draft's selection follows the text it was taken on across a refresh;
    // a selection whose words were all deleted becomes a note on the whole document.
    // Why: the guest selected words, not offsets — a member's edit elsewhere must not
    // move their note or refuse it ("документ изменился") while they are writing.
    const { draft } = get();
    let nextDraft = draft;
    if (draft?.selection) {
      const change = differingSpan(cur.content ?? '', resp.content);
      const { start, end } = draft.selection;
      const moved = change ? mapRangeThrough(change, start, end) : { start, end };
      nextDraft = { ...draft, selection: moved && { ...moved, content_sha256: resp.content_sha256 } };
      if (!moved) app.showToast(t('publicNoteFragmentGone'), 'info');
    }
    app.setCurrentDocument({
      ...cur, content: resp.content, tables_json: resp.tables_json, headings: resp.headings,
    });
    set({ servedSha: resp.content_sha256, draft: nextDraft });
    return true;
  },

  startNote(draft) {
    set({ draft, activeNoteId: null, draftSeq: get().draftSeq + 1 });
  },

  openNote(sessionId) {
    set({ activeNoteId: sessionId, draft: null });
  },

  closeThread() {
    set({ activeNoteId: null, draft: null });
  },

  setAuthorName(name) {
    set({ authorName: name });
    try { localStorage.setItem(NAME_KEY, name); } catch { /* per-viewer convenience only */ }
  },

  async submit(content) {
    const { documentId, draft, activeNoteId, notes } = get();
    const authorName = get().authorName.trim();
    const text = content.trim();
    if (!documentId || !text || !authorName || get().sending) return false;
    set({ sending: true });
    try {
      if (draft) {
        const create = () => createPublicNote(documentId, {
          author_name: authorName, content: text, ...(get().draft?.selection ?? {}),
        });
        let note;
        try {
          note = await create();
        } catch (e) {
          // 409: a member edited between the last refresh and this send — re-read the
          // text (which carries the selection over) and send once more.
          if (!(e instanceof HttpError && e.status === 409) || !(await get().refreshText(documentId))) throw e;
          note = await create();
        }
        set({ draft: null, activeNoteId: note.session_id });
      } else if (activeNoteId) {
        const thread = notes.find(n => n.session_id === activeNoteId);
        const last = thread?.messages[thread.messages.length - 1];
        await replyPublicNote(documentId, activeNoteId, {
          author_name: authorName, content: text, parent_id: last?.message_id ?? null,
        });
      } else {
        return false;
      }
      await get().load(documentId);
      return true;
    } catch (e) {
      // 409 here: the text changed again during the retry — the guest reloads.
      toastError(e instanceof HttpError && e.status === 409 ? 'publicNoteDocChanged' : 'failedToSendMessage');
      return false;
    } finally {
      set({ sending: false });
    }
  },

  async deleteMessage(sessionId, messageId) {
    const { documentId } = get();
    if (!documentId) return;
    try {
      await deletePublicNoteMessage(documentId, sessionId, messageId);
    } catch (e) {
      // 409: a reply arrived after the last refresh — the reloaded thread hides the trash.
      toastError(e instanceof HttpError && e.status === 409 ? 'publicNoteMessageHasReplies' : 'failedToDeleteMessage');
    }
    await get().load(documentId);
  },

  async deleteNote(sessionId) {
    const { documentId } = get();
    if (!documentId) return;
    try {
      await deletePublicNote(documentId, sessionId);
      if (get().activeNoteId === sessionId) set({ activeNoteId: null });
    } catch {
      toastError('failedToDeleteNote');
    }
    await get().load(documentId);
  },

  reset() {
    set({ ...INITIAL, authorName: get().authorName });
  },
}));

/** A guest may note here: comments are on, and the editor shows the document itself. */
export function publicCanNote(): boolean {
  const { allowComments } = usePublicNotesStore.getState();
  return allowComments && !useAppStore.getState().currentReference;
}

/** React twin of publicCanNote. */
export function usePublicCanNote(): boolean {
  const allowComments = usePublicNotesStore(s => s.allowComments);
  const onReference = useAppStore(s => !!s.currentReference);
  return allowComments && !onReference;
}

/**
 * The page should re-read the document text: the list was resolved against newer text
 * than the editor shows. Also while the guest composes over a selection —
 * refreshText carries it across the edit.
 */
export function publicDocIsBehind(): boolean {
  const { notesSha, servedSha } = usePublicNotesStore.getState();
  return !!notesSha && !!servedSha && notesSha !== servedSha;
}

/**
 * The anchored ranges the public editor may paint: resolved `ok` against the SAME text
 * the editor shows. A list resolved against newer text paints nothing — its offsets
 * would land on the wrong words. The note being composed paints too, like a member's
 * freshly created one: its offsets were taken from the shown text itself.
 */
export function paintablePublicNoteRanges(): { id: string; from: number; to: number }[] {
  const { notes, notesSha, servedSha, draft } = usePublicNotesStore.getState();
  const out: { id: string; from: number; to: number }[] = [];
  if (draft?.selection && draft.selection.content_sha256 === servedSha) {
    out.push({ id: PUBLIC_DRAFT_NOTE_ID, from: draft.selection.start, to: draft.selection.end });
  }
  if (!notesSha || notesSha !== servedSha) return out;
  for (const n of notes) {
    const a = n.anchor;
    if (a?.state === 'ok' && a.start != null && a.end != null) out.push({ id: n.session_id, from: a.start, to: a.end });
  }
  return out;
}
