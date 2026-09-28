/** Note chat view — chat interface for note sessions. */
// ARCH: note-chat view reuses message rendering but with independent store,
// no AI features (no streaming, no model selector, no fork navigation).
// INVARIANT: note-chat view never starts a completions turn  Why: note-chat reuses chat UI but has no AI features (no streaming/model/fork); it never posts a completion.
// SYSTEM: note-chat — note-chat UI subsystem reusing chat components

import { useEffect, useRef, useCallback, useLayoutEffect, useMemo } from 'react';
import { ChatComposer } from './chat/ChatComposer';
import { MessageBubble, type MessageBubbleActions } from './chat/MessageBubble';
import { copyWithToast } from './chat/shared/copy';
import { useNoteChatStore } from '../store/note-chat-store';
import { useNoteStore } from '../store/note-store';
import { useUIStore } from '../store/ui-store';
import { useAppStore } from '../store/app-store';
import { useIsProjectOwner } from '../hooks/useIsProjectOwner';
import { useTranslation } from '../i18n';
import { useSimpleVoiceRecording } from '../hooks/useSimpleVoiceRecording';
import { totalAttachmentBytes, attachmentBudgetExceeded, formatAttachmentMb } from '../utils/attachment-size';

const STICK_EPSILON = 48;

export function NoteChatView() {
  const { t } = useTranslation();
  const messages = useNoteChatStore(s => s.messages);
  const messagesLoading = useNoteChatStore(s => s.messagesLoading);
  const sendMessage = useNoteChatStore(s => s.sendMessage);
  const pendingInputFocus = useNoteChatStore(s => s.pendingInputFocus);
  const setPendingInputFocus = useNoteChatStore(s => s.setPendingInputFocus);
  const images = useNoteChatStore(s => s.pendingImages);
  const addPendingImage = useNoteChatStore(s => s.addPendingImage);
  const removePendingImage = useNoteChatStore(s => s.removePendingImage);
  const clearPendingImages = useNoteChatStore(s => s.clearPendingImages);
  const editMessage = useNoteChatStore(s => s.editMessage);
  const deleteMessage = useNoteChatStore(s => s.deleteMessage);
  const currentUser = useAppStore(s => s.currentUser);
  const showToast = useAppStore(s => s.showToast);

  // A project root (owner, or admin with a member row) = root for note messages:
  // full edit/delete power over any message (mirrors backend _is_project_owner →
  // is_project_root). Backend-computed capability flag; the backend is the
  // authority.
  const isProjectOwner = useIsProjectOwner();
  // A message is a non-leaf iff some other message points at it via parent_id.
  // Memoized on the messages array so the per-bubble hasChildren lookup is O(1).
  const parentIds = useMemo(
    () => new Set(messages.map(m => m.parent_id).filter((v): v is string => !!v)),
    [messages],
  );

  // Notes actions forwarded to the store-agnostic MessageBubble. Only
  // copy/edit/delete — no fork/regenerate/createDocument (those stay hidden).
  const handleCopy = useCallback((content: string) => {
    copyWithToast(content, showToast);
  }, [showToast]);
  const actions = useMemo<MessageBubbleActions>(() => ({
    onCopy: handleCopy,
    onEdit: editMessage,
    onDelete: deleteMessage,
  }), [handleCopy, editMessage, deleteMessage]);

  // SYSTEM: note-draft — the composer text lives in note-chat-store, PER-SESSION
  // (thread-scoped; a shared draft would leak text between note threads), so it
  // survives view unmount (back-to-list / tab switch / Esc). String selector →
  // value-stable. Dropped with the session, cleared on send and on store reset().
  const activeSessionId = useNoteChatStore(s => s.activeSessionId);
  const text = useNoteChatStore(s => (s.activeSessionId ? s.drafts[s.activeSessionId] ?? '' : ''));
  const setDraftForSession = useNoteChatStore(s => s.setDraftForSession);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);

  useEffect(() => {
    if (pendingInputFocus && !messagesLoading) {
      textareaRef.current?.focus();
      setPendingInputFocus(false);
    }
  }, [pendingInputFocus, messagesLoading, setPendingInputFocus]);

  // Voice transcription APPENDS to the thread's draft (read-append-write is safe:
  // the callback is not concurrent with typing).
  const onTranscribed = useCallback((transcribed: string) => {
    const { activeSessionId: sid, drafts, setDraftForSession: set } = useNoteChatStore.getState();
    if (!sid) return;
    const prev = drafts[sid] ?? '';
    set(sid, prev + (prev ? ' ' : '') + transcribed);
  }, []);
  const { recording, transcribing, toggleRecording } = useSimpleVoiceRecording(onTranscribed);

  const handleSend = useCallback(() => {
    if (!text.trim()) return;
    const sid = useNoteChatStore.getState().activeSessionId;
    if (!sid) return;
    const imgs = images.length > 0 ? images : undefined;
    sendMessage(text.trim(), imgs);
    // Optimistic clear (sendMessage is fire-and-forget with its own error toast —
    // draft restore on send failure is out of scope, mirrors the prior UX).
    setDraftForSession(sid, '');
    clearPendingImages();
    textareaRef.current?.focus();
  }, [text, images, sendMessage, clearPendingImages, setDraftForSession]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSend();
      return;
    }
    if (e.key === 'Escape') {
      e.preventDefault();
      const { messages, activeSessionId } = useNoteChatStore.getState();
      if (activeSessionId && messages.length === 0) {
        // ARCH: link strip is backend-authoritative
        // on delete; no client emit here.
        void useNoteChatStore.getState().deleteSession(activeSessionId);
      }
      // INVARIANT: Esc stays on the notes tab unless previousRightTab is
      // explicitly set (editor-initiated note creation only). Why: anchorless
      // notes created from the panel itself have no previous tab to restore;
      // restoring null would close the panel and leave a blank surface.
      const previousTab = useNoteStore.getState().previousRightTab;
      const docId = useAppStore.getState().currentDocument?.document_id ?? null;
      useNoteStore.getState().setActiveNoteThreadId(null, docId);
      useNoteChatStore.getState().setActiveSession(null);
      if (previousTab) {
        useNoteStore.getState().setPreviousRightTab(null);
        useUIStore.getState().setRightPanelTab(docId, previousTab);
      }
    }
  }, [handleSend]);

  const readImageFile = useCallback((file: File) => {
    // ARCH: note image budget reads the SHARED
    // maxAttachmentMb from app-store (single source, hydrated from /chat/models by
    // chat-store.loadModels), validated against the projected total of already-queued
    // images + the new file's raw bytes — mirroring ChatInput's logic.
    const maxMb = useAppStore.getState().maxAttachmentMb;
    const pending = totalAttachmentBytes(useNoteChatStore.getState().pendingImages);
    if (attachmentBudgetExceeded(pending, file.size, maxMb)) {
      useAppStore.getState().showToast(
        t('attachmentsExceedLimit', { used: formatAttachmentMb(pending + file.size), limit: maxMb }),
        'error',
      );
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === 'string') {
        addPendingImage(reader.result as string);
      }
    };
    reader.readAsDataURL(file);
  }, [t, addPendingImage]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) readImageFile(file);
      }
    }
  }, [readImageFile]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < STICK_EPSILON;
  }, []);

  useLayoutEffect(() => {
    if (messagesLoading) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    stickToBottomRef.current = true;
  }, [messagesLoading]);

  const lastMessageContent = messages[messages.length - 1]?.content;

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [messages.length, lastMessageContent]);

  if (messagesLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-ui-base text-text-dim">{t('loadingMessages')}</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div ref={containerRef} onScroll={handleScroll} className="flex-1 overflow-y-auto px-3 py-2">
        {messages.map((msg) => (
          <MessageBubble
            key={msg.message_id}
            message={msg}
            variant="note"
            isOwn={msg.author_id != null && msg.author_id === currentUser?.user_id}
            isOwner={isProjectOwner}
            hasChildren={parentIds.has(msg.message_id)}
            actions={actions}
          />
        ))}
      </div>
      <ChatComposer
        variant="note"
        value={text}
        onChange={e => {
          // Guard: the view is only rendered while a thread is open, but the
          // selector returns '' when activeSessionId is null — never write there.
          if (activeSessionId) setDraftForSession(activeSessionId, e.target.value);
        }}
        onSend={handleSend}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        isStreaming={false}
        canSend={text.trim().length > 0}
        placeholder={t('noteChatPlaceholder')}
        recording={recording}
        transcribing={transcribing}
        onToggleRecording={toggleRecording}
        images={images}
        onRemoveImage={removePendingImage}
        textareaRef={textareaRef}
      />
    </div>
  );
}
