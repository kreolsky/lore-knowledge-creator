/** The per-chat "unindexed" toggle — keeps a chat out of the agent's chat-recall search. */
import { apiClient } from '../../../api/client';
import { useAppStore } from '../../app-store';
import { t } from '../../../i18n';
import type { ChatState, Set, Get } from '../types';
import { threadRootOf } from '../branches-slice';

type UnindexedActions = Pick<ChatState, 'setChatUnindexed'>;

export function createUnindexedActions(set: Set, get: Get): UnindexedActions {
  return {
    async setChatUnindexed(value: boolean) {
      const sessionId = get().activeSessionId;
      // Ghost: no row yet — held client-side, sent on the create POST.
      if (!sessionId) {
        set({ ghostUnindexed: value });
        return;
      }
      // WHY: the server writes the flag to the WHOLE conversation (every branch +
      // compaction continuation), so every listed row of this thread flips with it.
      const root = threadRootOf(get, sessionId);
      const inThread = (id: string) => threadRootOf(get, id) === root;
      const previous = new Map(
        get().sessions.filter(s => inThread(s.session_id)).map(s => [s.session_id, !!s.unindexed]),
      );
      set(s => ({
        sessions: s.sessions.map(ss => previous.has(ss.session_id) ? { ...ss, unindexed: value } : ss),
      }));
      try {
        await apiClient.patch(`/chat/sessions/${sessionId}`, { unindexed: value });
      } catch {
        // No silent degradation: the mark must never claim a privacy the server did not store.
        set(s => ({
          sessions: s.sessions.map(ss => previous.has(ss.session_id)
            ? { ...ss, unindexed: previous.get(ss.session_id) }
            : ss),
        }));
        useAppStore.getState().showToast(t('chatUnindexedFailed'), 'error');
        return;
      }
      // WHY: the server also flips the compaction continuations, which share no
      // thread_id the client could match — a same-scope re-read (keeps the active
      // chat) replaces the list so no row keeps a flag the server changed.
      const projectId = useAppStore.getState().currentProject?.project_id;
      if (projectId) await get().loadSessions(projectId, get().documentId ?? undefined);
    },
  };
}
