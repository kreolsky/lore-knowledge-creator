/** The context-length nudge answer — advances the ACTIVE chat's `context_nudge_level`. */
import type { ChatSession } from '../../../types';
import { apiClient } from '../../../api/client';
import { useAppStore } from '../../app-store';
import { t } from '../../../i18n';
import type { ChatState, Set, Get } from '../types';
import { applySessionReparent } from '../session-parent';

type NudgeActions = Pick<ChatState, 'answerContextNudge'>;

export function createNudgeActions(set: Set, get: Get): NudgeActions {
  return {
    // WHY: a plain per-row counter — the server stores the number and derives
    // nothing (the live window exists only in the turn's stream), so the
    // client is the one that counts: level = k, the thresholds crossed at the
    // moment of the answer — one PATCH may use up several of the three.
    // Optimistic so the banner leaves at once; a failed PATCH rolls the row
    // back and toasts — a banner that stays down while the server kept the old
    // level would come back after a reload for an answer the person gave.
    // Ghost (no row): nothing to store — the next chat starts at level 0.
    async answerContextNudge(level: number) {
      const sessionId = get().activeSessionId;
      if (!sessionId) return;
      const previous = get().sessions.find(s => s.session_id === sessionId)?.context_nudge_level ?? null;
      set(s => ({
        sessions: s.sessions.map(ss => (ss.session_id === sessionId
          ? { ...ss, context_nudge_level: level }
          : ss)),
      }));
      let updated: ChatSession;
      try {
        updated = await apiClient.patch(`/chat/sessions/${sessionId}`, { context_nudge_level: level });
      } catch {
        set(s => ({
          sessions: s.sessions.map(ss => (ss.session_id === sessionId
            ? { ...ss, context_nudge_level: previous }
            : ss)),
        }));
        useAppStore.getState().showToast(t('chatContextNudgeFailed'), 'error');
        return;
      }
      // The server's row is the truth for the addressed row alone (an empty
      // thread set — a per-row field, unlike a reparent); last_message_at is
      // preserved by the shared reconcile.
      applySessionReparent(set, updated, new Set());
    },
  };
}
