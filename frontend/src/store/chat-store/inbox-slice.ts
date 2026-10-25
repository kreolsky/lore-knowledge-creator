/** Inbox slice — the ACTIVE chat's queue and its dsh status, plus the
 * per-session running set that marks EVERY working chat in the list.
 *
 * The queue is dsh's next-turn inbox (SYSTEM: harness-driver, session-agents.ts):
 * a busy send POSTs like any other and dsh queues it; the browser only
 * renders the inbox — seeded from the messages GET's snapshot and kept
 * current by the relayed `agent/inbox/spliced` frames (chat/inbox-queue.ts).
 * ✕ asks the plugin to remove the message; the chip leaves when the splice
 * frame arrives, never optimistically (the fold is index-based).
 *
 * INVARIANT: the queue fold and `dshRunning` belong to the active chat and
 * are reset on every activeSessionId transition (chat-store.ts watcher). Why:
 * splice indices are relative to ONE chat's inbox — a fold carried across a
 * switch applies one chat's removals to another chat's chips.
 * `runningSessions` is deliberately exempt: it is per-session, so it crosses
 * switches untouched (the list keeps marking a working chat the user left). */
// SYSTEM: chat-message-queue — the chat queue is dsh's inbox, rendered from snapshot + splices

import { apiClient } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import {
  EMPTY_FOLD, foldQueue, snapshotFrom, spliceFromFrame, withSnapshot, withSplice,
  type InboxFold,
} from '../../chat/inbox-queue';
import { registerChatResetHandler } from './reset-registry';
import type { ChatState, Get, Set } from './types';

let fold: InboxFold = EMPTY_FOLD;
registerChatResetHandler(() => { fold = EMPTY_FOLD; });

/** Seat the messages GET's snapshot (both load paths, session-rows.ts). */
export function seedInbox(set: Set, queue: unknown, seq: unknown): void {
  fold = withSnapshot(fold, snapshotFrom(queue, seq));
  set({ inboxQueue: foldQueue(fold) });
}

/** Fold one relayed frame of the ACTIVE chat: a next-turn splice moves the
 * queue, a `dsh_status` frame the busy state. Other frames are ignored. */
export function applyInboxFrame(set: Set, frame: unknown): void {
  const type = (frame as { type?: unknown } | null)?.type;
  if (type === 'dsh_status') {
    set({ dshRunning: (frame as { status?: unknown }).status === 'running' });
    return;
  }
  const splice = spliceFromFrame(frame);
  if (!splice) return;
  fold = withSplice(fold, splice);
  set({ inboxQueue: foldQueue(fold) });
}

/** Fold a `dsh_status` frame into the PER-SESSION running set — for ANY
 * chat, not just the active one: the chat list marks the chats whose turn
 * is still open while another chat is shown. A new Set is written only when
 * membership actually changes (dsh emits status often; a fresh reference
 * per frame re-renders every row). `dshRunning` is not touched here — it
 * stays the active chat's own gate. */
export function applyRunningStatus(get: Get, set: Set, sessionId: string, frame: unknown): void {
  const running = (frame as { status?: unknown } | null)?.status === 'running';
  const cur = get().runningSessions;
  if (cur.has(sessionId) === running) return;
  const next = new Set(cur);
  if (running) next.add(sessionId);
  else next.delete(sessionId);
  set({ runningSessions: next });
}

/** A project-WS gap may have swallowed the `idle` that ended a turn: drop
 * the set wholesale rather than leave a mark that never clears (no mark is
 * less wrong than a permanent false one; the reload-seed closes the gap). */
export function clearRunningSessions(set: Set): void {
  set({ runningSessions: new Set() });
}

/** The activeSessionId transition: the next chat starts with no queue and
 * idle until its own snapshot / status arrive. */
export function resetInbox(set: Set): void {
  fold = EMPTY_FOLD;
  set({ inboxQueue: [], dshRunning: false });
}

export function createInboxSlice(_set: Set, get: Get): Pick<ChatState, 'discardQueued'> {
  return {
    async discardQueued(messageId) {
      const sessionId = get().activeSessionId;
      if (!sessionId) return;
      try {
        // `{removed: false}` = claimed in the meantime: its turn running in
        // the transcript is the feedback, so no toast (operator ruling).
        await apiClient.post(`/chat/sessions/${sessionId}/inbox/discard`, { message_id: messageId });
      } catch {
        useAppStore.getState().showToast(t('queueDiscardFailed'), 'error');
      }
    },
  };
}
