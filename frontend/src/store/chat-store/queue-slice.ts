/** Message-queue slice — follow-up chips typed while a turn streams.
 *
 * The queue is per-session and lives entirely in chat-store; the backend never learns it
 * exists. `sendMessage` routes here while `streaming !== null`; at a clean turn-end ONE
 * coalesced message auto-fires; abort/error/halted restore the text to the composer. */
// SYSTEM: chat-message-queue — per-session follow-up message queue

import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import { joinQueued } from '../../chat/queue-join';
import { appendDraft } from './misc-slice';
import type { Get, Set, TurnEndReason } from './types';

export interface QueueSlice {
  enqueueMessage: (sessionId: string, text: string) => void;
  removeQueued: (sessionId: string, index: number) => void;
  clearQueued: (sessionId: string) => void;
  flushQueued: (sessionId: string) => Promise<void>;
  restoreQueued: (sessionId: string) => void;
}

export function createQueueSlice(set: Set, get: Get): QueueSlice {
  return {
    enqueueMessage(sessionId, text) {
      // Trim+dedupe-free at join time; here we keep the raw chip (the user may edit it
      // in place via removeQueued + retype). A blank chip is still stored so the chip
      // row reflects exactly what the user entered (joinQueued drops blanks on flush).
      set(s => ({
        queued: { ...s.queued, [sessionId]: [...(s.queued[sessionId] ?? []), text] },
      }));
    },

    removeQueued(sessionId, index) {
      set(s => {
        const cur = s.queued[sessionId];
        if (!cur) return {};
        const next = cur.filter((_, i) => i !== index);
        const queued = { ...s.queued };
        if (next.length === 0) delete queued[sessionId];
        else queued[sessionId] = next;
        return { queued };
      });
    },

    clearQueued(sessionId) {
      set(s => {
        if (!(sessionId in s.queued)) return {};
        const queued = { ...s.queued };
        delete queued[sessionId];
        return { queued };
      });
    },

    async flushQueued(sessionId) {
      // A turn may have started between the schedule and this task (the user sent
      // manually after the switch). Leave the queue intact — it flushes at the next
      // clean end. Checked before switching/clearing so nothing is lost.
      if (get().streaming) return;
      const parts = get().queued[sessionId];
      if (!parts?.length) return;
      // Session deleted while queued: the flush target is gone. Drop the
      // queue and tell the user.
      const session = get().sessions.find(s => s.session_id === sessionId);
      if (!session) {
        get().clearQueued(sessionId);
        useAppStore.getState().showToast(t('chatQueueDropped'), 'info');
        return;
      }
      const text = joinQueued(parts);
      get().clearQueued(sessionId);
      // ARCH: flush is session-owned, not view-owned. The single-session streaming model
      // renders a turn ONLY for the active session (optimistic insert + frame deltas write
      // into get().messages / get().streaming), so a follow-up for a non-active session
      // must bring the user to it before sending. The follow-up then streams in its
      // owning session — the honest realization of "the queue is a promise to send".
      if (get().activeSessionId !== sessionId) {
        get().setActiveSession(sessionId);
        await get().loadMessages(sessionId);
      }
      // Re-check: a manual send may have raced in during the (awaited) session switch.
      if (get().streaming) return;
      await get().sendMessage(text);
    },

    restoreQueued(sessionId) {
      const parts = get().queued[sessionId];
      if (!parts?.length) return;
      // Restore into the composer only when the owning session is the active one —
      // dumping the joined text into the composer while another session is shown
      // would send it into the wrong chat. For the cross-session case the chips are
      // left intact so the user finds them on return.
      if (get().activeSessionId !== sessionId) return;
      // Append through the shared restore rule (appendDraft, where the
      // invariant and its Why live): never overwrite — the user may have
      // typed more since the chips were queued.
      const restored = joinQueued(parts);
      appendDraft(get, restored);
      get().clearQueued(sessionId);
    },
  };
}

/**
 * Drain the queue at a turn's end (the WS terminal frame, the gap-close of a turn
 * whose terminal was lost, or a POST that never opened the turn). A clean finish
 * auto-fires ONE coalesced message; any intervention/failure restores instead. Reads
 * nothing if the session has no queue.
 *
 * INVARIANT: only `reason === 'done'` auto-fires; 'halted' (a budget halt) does NOT.
 * Why: a halt is a deliberate stop with a result — the user should press the halt
 * card's Continue (a visible user message) rather than have a queued follow-up
 * silently resume the turn the guard just stopped. Auto-firing on 'halted' would
 * also re-feed the empty-completion death loop, which is why there is no
 * "continue to retry" invitation. The Continue button is the only manual release.
 */
export function scheduleTurnEndFlush(get: Get, sessionId: string, reason: TurnEndReason): void {
  const parts = get().queued?.[sessionId];
  if (!parts?.length) return;
  // INVARIANT: the drain runs after runCompletion's finally.
  // Why: the finally's set(flushStreaming) would wipe the NEXT turn's streaming slot if
  // the queued send started first. A macrotask, never a microtask: the finally sits
  // behind an await chain that a queued microtask does not reliably outrun.
  setTimeout(() => {
    if (reason === 'done') {
      void get().flushQueued(sessionId);
    } else {
      get().restoreQueued(sessionId);
    }
  }, 0);
}
