/**
 * ONE home for the message CRUD request layer shared by the AI chat-store and
 * the note-chat store.
 *
 * // SYSTEM: chat-message-crud — scope-discriminated message load/edit/delete
 *
 * Both stores hit the SAME endpoints with the SAME types
 * (`/chat/sessions/{id}/messages`, `/chat/messages/{id}`); before this module
 * each carried its own copy of the request + liveness-guard + error-toast
 * triplet, and the copies drifted. The request layer lives HERE, exactly once;
 * the scope discriminator is the caller-supplied callback set — each store
 * keeps its own state mutations (chat: tree/streaming/gate flags; note:
 * session-preview re-derivation) and its own deliberate tail behavior.
 *
 * NOT shared, deliberately (do not "finish the merge" — the differences are
 * behavioral, not accidental):
 * - deleteMessage tail: chat runs a client-side BFS cascade over the message
 *   tree; note relies on the realtime `deleted` frame carrying `message_ids`
 *   (see SYSTEM: note-realtime).
 * - deleteSession strategy: note is optimistic + snapshot-restore; chat is
 *   post-success + ghost landing + ui-store pointer clear.
 * - sendMessage: chat starts a streaming agent turn (completions); note does a plain POST
 *   and derives session previews from the result.
 */
import { stripFrames } from './chat-store/conversation-feed';
import { apiClient } from '../api/client';
import { useAppStore } from './app-store';
import { t } from '../i18n';
import type { ChatMessage } from '../types';

/**
 * Scope discriminator for loadMessagesFor — the live-session liveness guard
 * plus the two terminals. `activeSessionId()` is read AFTER the await, so a
 * session switch while the fetch was in flight makes the result stale and the
 * load is dropped without touching state.
 */
export interface MessageLoadScope {
  activeSessionId: () => string | null;
  /** Success terminal — commit messages + clear the loading flag. */
  onLoaded: (messages: ChatMessage[]) => void;
  /** Error terminal — invoked only when this fetch still owns the session. */
  onLoadError: () => void;
  /** Raw rows BEFORE the timeline fold — the chat store hands them to the
   * assembler (replaceWindow; the note store passes nothing). Invoked before
   * hydration because the fold drops the rows' `frames` key; only for a fetch
   * that still owns the session. */
  onRows?: (rows: ChatMessage[]) => void;
}

/**
 * GET a session's message list with the shared staleness guard and error
 * surfacing. The toast on failure is UNCONDITIONAL (a failed load is worth
 * surfacing even when superseded); the error terminal is liveness-gated so a
 * stale rejection cannot clobber the new session's loading state.
 */
export async function loadMessagesFor(sessionId: string, scope: MessageLoadScope): Promise<void> {
  try {
    const raw: ChatMessage[] = await apiClient.get(`/chat/sessions/${sessionId}/messages`);
    // The reload half of the ONE timeline: the raw rows go to the assembler
    // (replaceWindow over the frames the backend replayed + minted), and the
    // rows enter the store without them. A frameless row renders from its own
    // fields, as it does live.
    // Staleness guard: user may have switched sessions while fetch was in-flight.
    // It covers the raw rows too — onRows seats the assembler window and the
    // streaming slot, which belong to the shown session only.
    if (scope.activeSessionId() !== sessionId) return;
    scope.onRows?.(raw);
    const messages = raw.map(stripFrames);
    scope.onLoaded(messages);
  } catch {
    if (scope.activeSessionId() === sessionId) scope.onLoadError();
    useAppStore.getState().showToast(t('failedToLoadMessages'), 'error');
  }
}

/**
 * PATCH a message's content. Returns the server-updated message, or null on
 * failure (toast surfaced here — no silent degradation). The caller owns the
 * state mutation and any derived updates (note session previews).
 */
export async function patchMessageContent(messageId: string, content: string): Promise<ChatMessage | null> {
  try {
    return await apiClient.patch(`/chat/messages/${messageId}`, { content });
  } catch {
    useAppStore.getState().showToast(t('failedToEditMessage'), 'error');
    return null;
  }
}

/**
 * DELETE a message row. Returns false on failure (toast surfaced here); on
 * true the CALLER owns the removal semantics (chat: BFS cascade + sibling
 * cleanup; note: single-row removal + preview re-derivation).
 */
export async function deleteMessageById(messageId: string): Promise<boolean> {
  try {
    await apiClient.delete(`/chat/messages/${messageId}`);
    return true;
  } catch {
    useAppStore.getState().showToast(t('failedToDeleteMessage'), 'error');
    return false;
  }
}
