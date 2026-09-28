/** Zustand store for note-chat sessions — isolated from AI chat-store. */
// ARCH: note-chat-store — isolated Zustand store for note-chat sessions.
// Reuses message UI components but with independent session state,
// preventing collision with the AI chat-store.activeSessionId.
// SYSTEM: note-chat — note-chat store for user-only message threads

import { create } from 'zustand';
import { ChatSession, ChatMessage } from '../types';
import { apiClient } from '../api/client';
import { useAppStore } from './app-store';
import { createResourceCache } from '../api/swr-cache';
import { registerLogoutHandler, getLogoutEpoch } from './logout-handlers';
import { upsertByKey } from './chat-store/upsert';
import { loadMessagesFor, patchMessageContent, deleteMessageById } from './chat-message-crud';
import { t } from '../i18n';

// ARCH: SWR cache for the note-session list (see SYSTEM: swr-cache). Unlike the AI
// chat-store this store has no in-flight dedup / terminal INVARIANT, so SWR lands as
// the first caching layer: a re-open paints the last-seen notes instantly, then the
// revalidate fetch reconciles. sessionsLoading reflects the revalidate fetch as before.
const noteSessionsCache = createResourceCache<ChatSession[]>();

/** Test-only: reset the note-sessions SWR cache. */
export function clearNoteSessionsCache(): void {
  noteSessionsCache.clear();
}

// Self-register the note sessions cache clear into the logout
// registry so setCurrentUser(null) drops it (bridge mirrors registerAppBridge).
registerLogoutHandler(clearNoteSessionsCache);

// SYSTEM: note-realtime upsert — race-safe dedup helper shared by the optimistic
// create/send paths and the realtime apply helpers. The actor's own mutation AND
// the realtime frame it also receives can land in either order; both must converge
// to a single entry (replace if the id exists, else prepend/append). The generic
// `upsertByKey` lives in chat-store/upsert.ts (a shared primitive); the
// session-list variant below PREPENDS on new (newest-first) — note-list-specific.
function _upsertSession(sessions: ChatSession[], session: ChatSession): ChatSession[] {
  const idx = sessions.findIndex(s => s.session_id === session.session_id);
  // Prepend when new (the note list is newest-first); replace in place when the
  // realtime frame / optimistic create already added it (race-safe dedup).
  if (idx === -1) return [session, ...sessions];
  const next = [...sessions];
  next[idx] = session;
  return next;
}

export interface NoteAnchor {
  offsetStart: number;
  offsetEnd: number;
  relStart?: string;
  relEnd?: string;
}

interface NoteChatState {
  sessions: ChatSession[];
  // WHY: WHICH scope the rows in `sessions` belong to — the same key the SWR cache
  // uses (`${projectId}:${documentId}:note`). Why: `sessions` is only meaningful
  // together with `sessionsScope` — the previous document's rows must never render
  // under the open one. Scope transitions are SYNCHRONOUS at loadSessions entry
  // (stale rows dropped / cached rows painted before the fetch resolves), and
  // DocumentPage's already-committed guard branch reconciles through ensureSessions —
  // the pre-commit nav path (Header commits the document BEFORE navigating) skips
  // the whole open bundle, so that branch is the only note-session loader on it.
  sessionsScope: string | null;
  // Internal: the latest REQUESTED scope key, recorded synchronously at loadSessions
  // entry — the out-of-order guard. A late-resolving fetch applies its rows only if
  // its scope is still this (rapid A→B with A landing last must not repaint A's
  // rows under open doc B; the guard-branch fetch has no `cancelled` cleanup).
  // Cleared by reset(). The SWR cache write is always allowed — it is keyed per scope.
  lastRequestedSessionsScope: string | null;
  activeSessionId: string | null;
  messages: ChatMessage[];
  messagesLoading: boolean;
  // WHY: true while the note-session list for the current scope is in flight.
  // Gates the NotesPanel empty state so "no notes yet" never flashes before the
  // /chat/sessions?is_note fetch lands. Empty state ≠ loading state. Why: flashing "no notes" during load violates no-silent-degradation.
  sessionsLoading: boolean;
  pendingInputFocus: boolean;
  pendingImages: string[];
  // SYSTEM: note-draft — PER-SESSION composer drafts (thread-scoped; a shared draft
  // would leak text between distinct note threads, unlike AI-chat's session-agnostic
  // prompt). Survives NoteChatView unmount (back-to-list / tab switch / Esc).
  // In-memory only: dropped with the session, cleared by reset() — F5 drops it.
  drafts: Record<string, string>;

  loadSessions: (projectId: string, documentId: string) => Promise<void>;
  /** Scope-reconciling loader for the already-committed doc-open path: no-ops (zero
   * fetch) when the store's sessionsScope already matches, otherwise loadSessions.
   * See the sessionsScope WHY note above. */
  ensureSessions: (projectId: string, documentId: string) => Promise<void>;
  hydrateSessions: (projectId: string, documentId: string, sessions: ChatSession[]) => void;
  createNoteSession: (opts: {
    projectId: string;
    documentId: string;
    referenceId?: string;
    anchor?: NoteAnchor;
  }) => Promise<ChatSession>;
  setActiveSession: (sessionId: string | null) => void;
  deleteSession: (sessionId: string) => Promise<void>;
  loadMessages: (sessionId: string) => Promise<void>;
  sendMessage: (content: string, images?: string[]) => Promise<void>;
  editMessage: (messageId: string, content: string) => Promise<void>;
  deleteMessage: (messageId: string) => Promise<void>;
  setPendingInputFocus: (v: boolean) => void;
  addPendingImage: (dataUrl: string) => void;
  removePendingImage: (index: number) => void;
  clearPendingImages: () => void;
  setDraftForSession: (sessionId: string, v: string) => void;

  // See SYSTEM: note-realtime — idempotent remote-apply
  // helpers consumed by useNoteCrud's collab-note-* subscriptions. Dedup by id so the
  // actor's own client (which also receives the broadcast frame) does not double-apply.
  upsertSession: (session: ChatSession) => void;
  removeSession: (sessionId: string) => void;
  applyMessageChange: (payload: {
    action: string;
    session_id: string;
    message?: ChatMessage;
    message_ids?: string[];
    preview?: { first?: string | null; last?: string | null; count?: number; last_at?: string | null };
  }) => void;

  reset: () => void;
}

export const useNoteChatStore = create<NoteChatState>((set, get) => ({
  sessions: [],
  sessionsScope: null,
  lastRequestedSessionsScope: null,
  activeSessionId: null,
  messages: [],
  messagesLoading: false,
  sessionsLoading: false,
  pendingInputFocus: false,
  pendingImages: [],
  drafts: {},

  async loadSessions(projectId: string, documentId: string) {
    const scopeKey = `${projectId}:${documentId}:note`;
    // SWR pre-gate paint: show the last-seen notes for this scope instantly while the
    // revalidate fetch below reconciles. NOT a terminal — sessionsLoading still gates.
    const cached = noteSessionsCache.get(scopeKey);
    // INVARIANT(scope-reconcile): the transition to a new scope is SYNCHRONOUS at
    // entry. Why: NotesPanel only gates the EMPTY list behind sessionsLoading, so
    // keeping the previous document's rows during the fetch would render THEM under
    // the new doc — and forever on a no-cache fetch failure. Drop stale rows (or
    // paint this scope's cached ones) NOW, engage the spinner gate, and on failure
    // leave an honest empty list + toast. On scope match keep today's cached
    // pre-paint as-is.
    const patch: Partial<NoteChatState> = {
      // Out-of-order guard: record the latest requested scope synchronously; the
      // success/finally sets below apply only while this call's scope is still the
      // last requested.
      lastRequestedSessionsScope: scopeKey,
      sessionsLoading: true,
    };
    if (get().sessionsScope !== scopeKey) {
      patch.sessions = cached ?? [];
      patch.sessionsScope = scopeKey;
    } else if (cached) {
      patch.sessions = cached;
    }
    set(patch);
    // Capture the logout generation BEFORE the await: a soft logout while this fetch is
    // in flight bumps the epoch, and we must NOT write the (prior user's) result back
    // into the cache/store after reset undid it (a logout TOCTOU).
    const startEpoch = getLogoutEpoch();
    try {
      const params = new URLSearchParams({
        project_id: projectId,
        document_id: documentId,
        is_note: 'true',
      });
      const sessions: ChatSession[] = await apiClient.get(`/chat/sessions?${params}`);
      if (getLogoutEpoch() !== startEpoch) return; // TOCTOU: a logout happened — drop the stale write
      // Always allowed: the cache write is correctly keyed per scope even when the
      // store set below is superseded.
      noteSessionsCache.seed(scopeKey, sessions);
      if (get().lastRequestedSessionsScope === scopeKey) set({ sessions });
    } catch {
      useAppStore.getState().showToast(t('failedToLoadNotes'), 'error');
    } finally {
      // Same latest-requested guard: a superseded call must not release the spinner
      // gate while the superseding fetch is still in flight.
      if (get().lastRequestedSessionsScope === scopeKey) set({ sessionsLoading: false });
    }
  },

  async ensureSessions(projectId: string, documentId: string) {
    if (get().sessionsScope === `${projectId}:${documentId}:note`) return;
    await get().loadSessions(projectId, documentId);
  },

  /** Commit an already-fetched note-session list for a scope (no request).
   *
   * The caller obtained the list in a bundle that also carried the document
   * (GET /documents/open/{id}), so re-running loadSessions would spend a second
   * round trip on data already in hand. Writes the same cache entry loadSessions
   * writes, so a later revalidate for this scope paints from it instantly. */
  hydrateSessions(projectId, documentId, sessions) {
    const scopeKey = `${projectId}:${documentId}:note`;
    noteSessionsCache.seed(scopeKey, sessions);
    // Scope + last-requested committed TOGETHER with the rows (same set()): a
    // loadSessions still in flight for another scope is superseded by this bundle
    // commit and must not repaint over it.
    set({
      sessions,
      sessionsScope: scopeKey,
      lastRequestedSessionsScope: scopeKey,
      sessionsLoading: false,
    });
  },

  async createNoteSession(opts: {
    projectId: string;
    documentId: string;
    referenceId?: string;
    anchor?: NoteAnchor;
  }) {
    const body: Record<string, unknown> = {
      project_id: opts.projectId,
      // WHY: parent resolution happens in the CALLER (createNote in
      // Why: the store cannot know which editor is focused; only the caller has that context.
      // markdown-actions.ts), which passes referenceId ONLY when the focused editor
      // is rendering a reference (getFocusedIsReference()). Do NOT assume a note
      // attaches to the open reference just because one is present — in split mode
      // the document column (and a single editor showing a document) must keep
      // referenceId undefined so it parents to the document. Here we only honor what
      // the caller decided: referenceId → reference, else documentId → document.
      document_id: opts.referenceId || opts.documentId,
      is_note: true,
    };
    if (opts.anchor) {
      body.anchor_offset_start = opts.anchor.offsetStart;
      body.anchor_offset_end = opts.anchor.offsetEnd;
      if (opts.anchor.relStart) body.anchor_rel_start = opts.anchor.relStart;
      if (opts.anchor.relEnd) body.anchor_rel_end = opts.anchor.relEnd;
    }
    // TOCTOU: capture the logout generation before the POST — a soft logout while
    // it is in flight resets the store, and the resolving POST must not write the prior
    // user's freshly-created note session back into it.
    const startEpoch = getLogoutEpoch();
    const session: ChatSession = await apiClient.post('/chat/sessions', body);
    if (getLogoutEpoch() !== startEpoch) {
      // INVARIANT: on a logout-epoch mismatch createNoteSession SKIPS the store insert
      // but still RETURNS the session (unlike chat-store's createSession, whose null is
      // the established lazy-create no-op signal). Why: markdown-actions must finalize
      // the persisted `note:tempId` link in the document text via session_id — a null
      // here would leave the temp link in user content. The row exists server-side
      // under the prior user's id; the next user's loadSessions never returns it.
      return session;
    }
    set(s => ({
      // WHY: UPSERT by session_id, not blind
      // prepend. Why: the realtime `note_session_created` frame the actor ALSO
      // receives can win the race (WS onmessage fires before this fetch promise
      // resolves), in which case upsertSession already added the session — a blind
      // prepend here would then create a DUPLICATE list entry with the same id
      // (the reported "two identical notes" bug; refresh hides it via loadSessions).
      sessions: _upsertSession(s.sessions, session),
      activeSessionId: session.session_id,
      messages: [],
      messagesLoading: false,
    }));
    return session;
  },

  setActiveSession(sessionId: string | null) {
    set({
      activeSessionId: sessionId,
      messages: [],
      messagesLoading: false,
    });
    if (sessionId) {
      get().loadMessages(sessionId);
    }
  },

  async deleteSession(sessionId: string) {
    // INVARIANT: optimistic removal — the session disappears from the list
    // synchronously, before the DELETE request resolves. Why: leaving it
    // visible until the response causes a red anchorless card to flash in
    // the panel when exiting an empty new note (back/Esc with 0 messages).
    // On API failure we restore the snapshot and surface a toast.
    const snapshot = {
      sessions: get().sessions,
      activeSessionId: get().activeSessionId,
      messages: get().messages,
      // The draft rides the snapshot: a failed delete must restore it too.
      drafts: get().drafts,
    };
    const wasActive = snapshot.activeSessionId === sessionId;
    set(s => ({
      sessions: s.sessions.filter(ss => ss.session_id !== sessionId),
      activeSessionId: wasActive ? null : s.activeSessionId,
      messages: wasActive ? [] : s.messages,
      drafts: Object.fromEntries(Object.entries(s.drafts).filter(([id]) => id !== sessionId)),
    }));
    try {
      await apiClient.delete(`/chat/sessions/${sessionId}`);
    } catch {
      set(snapshot);
      useAppStore.getState().showToast(t('failedToDeleteNote'), 'error');
    }
  },

  async loadMessages(sessionId: string) {
    set({ messagesLoading: true });
    // Shared request layer (chat-message-crud): staleness guard + error toast
    // live there, once, for both stores; the terminals here are note-simple.
    await loadMessagesFor(sessionId, {
      activeSessionId: () => get().activeSessionId,
      onLoaded: messages => set({ messages, messagesLoading: false }),
      onLoadError: () => set({ messagesLoading: false }),
    });
  },

  async sendMessage(content: string, images?: string[]) {
    const { activeSessionId } = get();
    if (!activeSessionId || !content.trim()) return;

    const activePath = get().messages;
    const parentId = activePath.length > 0 ? activePath[activePath.length - 1].message_id : null;

    try {
      const message: ChatMessage = await apiClient.post(
        `/chat/sessions/${activeSessionId}/messages`,
        { content: content.trim(), parent_id: parentId, images },
      );
      set(s => {
        // WHY: idempotent vs the realtime
        // `note_message_changed(action=added)` frame the actor ALSO receives. UPSERT
        // the message by message_id and DERIVE the preview/count absolutely from the
        // resulting messages array (not a blind append + increment) — otherwise, when
        // the WS frame wins the race and applyMessageChange already added the message,
        // this blind append would create a DUPLICATE message + an over-counted pill.
        const messages = upsertByKey(s.messages, message, 'message_id');
        const trimmed = content.trim();
        const nowIso = message.created_at || new Date().toISOString();
        const sessions = s.sessions.map(ss => {
          if (ss.session_id !== activeSessionId) return ss;
          const first = messages[0];
          const last = messages[messages.length - 1];
          return {
            ...ss,
            first_message_preview: first ? (first.content ?? ss.first_message_preview) : (ss.first_message_preview ?? trimmed),
            last_message_preview: last ? (last.content ?? trimmed) : trimmed,
            message_count: messages.length,
            last_message_at: last ? (last.created_at ?? nowIso) : nowIso,
            updated_at: nowIso,
          };
        });
        return { messages, sessions };
      });
    } catch {
      useAppStore.getState().showToast(t('failedToSendMessage'), 'error');
    }
  },

  async editMessage(messageId: string, content: string) {
    // Shared request layer (chat-message-crud) owns PATCH + error toast; the
    // note-specific preview re-derivation stays here.
    const updated = await patchMessageContent(messageId, content);
    if (!updated) return;
    set(s => {
      const messages = s.messages.map(m => m.message_id === messageId ? updated : m);
      // WHY: re-derive the owning session's preview fields from the updated array
      // so the list card stays fresh without a full loadSessions refetch. The card
      // reads first_message_preview; editMessage is only invoked from an open thread
      // (NoteChatView), so messages is loaded and first/last indices are valid.
      const sessionId = s.activeSessionId;
      const sessions = sessionId
        ? s.sessions.map(ss => {
            if (ss.session_id !== sessionId) return ss;
            const first = messages[0];
            const last = messages[messages.length - 1];
            return {
              ...ss,
              first_message_preview: first ? (first.content ?? ss.first_message_preview) : ss.first_message_preview,
              last_message_preview: last ? (last.content ?? ss.last_message_preview) : ss.last_message_preview,
            };
          })
        : s.sessions;
      return { messages, sessions };
    });
  },

  async deleteMessage(messageId: string) {
    // Shared request layer (chat-message-crud) owns DELETE + error toast.
    if (!(await deleteMessageById(messageId))) return;
    // WHY: remove ONLY the target row here.
    // For an owner cascade, the realtime `applyMessageChange(action=deleted,
    // message_ids=[full subtree])` frame carries the whole set and is applied
    // idempotently by applyMessageChange — no client-side BFS needed. A non-owner
    // author can only delete a single leaf, so the target is the whole set.
    set(s => {
      const messages = s.messages.filter(m => m.message_id !== messageId);
      const sessionId = s.activeSessionId;
      const sessions = sessionId
        ? s.sessions.map(ss => {
            if (ss.session_id !== sessionId) return ss;
            const first = messages[0];
            const last = messages[messages.length - 1];
            return {
              ...ss,
              first_message_preview: first ? (first.content ?? null) : null,
              last_message_preview: last ? (last.content ?? null) : null,
              message_count: messages.length,
              last_message_at: last ? (last.created_at ?? null) : null,
            };
          })
        : s.sessions;
      return { messages, sessions };
    });
  },

  setPendingInputFocus(v: boolean) {
    set({ pendingInputFocus: v });
  },

  addPendingImage(dataUrl: string) {
    set(s => ({ pendingImages: [...s.pendingImages, dataUrl] }));
  },

  removePendingImage(index: number) {
    set(s => ({ pendingImages: s.pendingImages.filter((_, i) => i !== index) }));
  },

  clearPendingImages() {
    set({ pendingImages: [] });
  },

  // Per-session composer draft (see NoteChatState.drafts).
  setDraftForSession(sessionId: string, v: string) {
    set(s => ({ drafts: { ...s.drafts, [sessionId]: v } }));
  },

  // ── note-realtime idempotent remote-apply ──

  upsertSession(session: ChatSession) {
    set(s => ({ sessions: _upsertSession(s.sessions, session) }));
  },

  removeSession(sessionId: string) {
    set(s => {
      const wasActive = s.activeSessionId === sessionId;
      return {
        sessions: s.sessions.filter(ss => ss.session_id !== sessionId),
        // Clear the open thread if it was the deleted one.
        ...(wasActive ? { activeSessionId: null, messages: [] } : {}),
        // The thread's draft dies with it (a resurrected session id never returns).
        drafts: Object.fromEntries(Object.entries(s.drafts).filter(([id]) => id !== sessionId)),
      };
    });
  },

  applyMessageChange(payload) {
    const { action, session_id, message, message_ids, preview } = payload;
    set(s => {
      let messages = s.messages;
      // Only mutate the open thread's messages when it IS the changed session.
      if (s.activeSessionId === session_id) {
        if (action === 'deleted') {
          const ids = new Set(message_ids ?? []);
          messages = s.messages.filter(m => !ids.has(m.message_id));
        } else if (message) {
          // Idempotent upsert by message_id (added | edited).
          messages = upsertByKey(s.messages, message, 'message_id');
        }
      }
      // Always patch the owning session's preview from the derived `preview` (the
      // actor already set the same values → idempotent, no double-count).
      const sessions = s.sessions.map(ss => {
        if (ss.session_id !== session_id || !preview) return ss;
        return {
          ...ss,
          first_message_preview: preview.first ?? ss.first_message_preview,
          last_message_preview: preview.last ?? ss.last_message_preview,
          message_count: typeof preview.count === 'number' ? preview.count : ss.message_count,
          last_message_at: preview.last_at ?? ss.last_message_at,
        };
      });
      return { messages, sessions };
    });
  },

  reset() {
    set({
      sessions: [],
      sessionsScope: null,
      lastRequestedSessionsScope: null,
      activeSessionId: null,
      messages: [],
      messagesLoading: false,
      sessionsLoading: false,
      pendingInputFocus: false,
      pendingImages: [],
      // Logout hygiene: the prior user's note drafts never render after a
      // same-tab re-login.
      drafts: {},
    });
  },
}));

// Reset the note-chat store on soft logout so the prior user's
// note sessions/messages can't render after a same-tab re-login (cache clear alone
// doesn't touch store state). See the parallel registration in chat-store.ts.
registerLogoutHandler(() => useNoteChatStore.getState().reset());
