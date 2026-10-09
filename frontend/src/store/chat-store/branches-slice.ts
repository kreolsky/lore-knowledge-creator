/** Branch slice — the switcher's data + opening a branch.
 *
 * SYSTEM: chat-branch-sessions — a branch is its own chat_sessions row in the
 * same thread: the "1/N ◄►" switcher no longer picks in-tree siblings — it
 * OPENS sessions. This slice owns the projection fetch (`/forks`,
 * staleness-guarded against the active pointer) and the open (= switching)
 * action: the thread root's active_branch_id is PATCHed so the chat list
 * previews the last-opened branch, then the branch becomes the active
 * session. */
import type { ChatSession } from '../../types';
import { apiClient } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import type { ChatState, Set, Get } from './types';

type BranchesSlice = Pick<ChatState, 'loadForks' | 'openBranch'>;

// A branch is bound to its thread for life, so
// branch → root is an immutable server FACT, not cacheable UI state — it
// survives logout/reset by design and can never go stale. Remembered on
// every confirmed open because `sessions` previews only the last-OPENED
// branch: a switch between two never-listed siblings has NO store row on
// either side to read the root from.
const threadRootByBranch = new Map<string, string>();

/** The thread ROOT of a session: its store row's thread_id, else a
 * remembered branch→root fact, else itself (a pre-threading row, or a root,
 * is its own thread). */
export function threadRootOf(get: Get, sessionId: string): string {
  const row = get().sessions.find(s => s.session_id === sessionId);
  return row?.thread_id || threadRootByBranch.get(sessionId) || sessionId;
}

/** Remember a confirmed branch→root fact (immutable server-side). */
function rememberThreadRoot(sessionId: string, rootId: string): void {
  threadRootByBranch.set(sessionId, rootId);
}

/** The session list after `branch` became its thread's open branch: the
 * thread is ONE list entry (the server previews the last-opened branch), so
 * the branch REPLACES whatever row stood for its thread. */
export function withThreadRow(sessions: ChatSession[], branch: ChatSession): ChatSession[] {
  const root = branch.thread_id || branch.session_id;
  rememberThreadRoot(branch.session_id, root);
  return [
    branch,
    ...sessions.filter(s => (s.thread_id || s.session_id) !== root),
  ];
}

export function createBranchesSlice(set: Set, get: Get): BranchesSlice {
  return {
    async loadForks(sessionId: string) {
      let entries: ChatState['forks'];
      try {
        entries = await apiClient.get(`/chat/sessions/${sessionId}/forks`);
      } catch {
        // No silent degradation: an unreadable switcher is surfaced, never an
        // empty one posing as "no forks" (only for the session still shown).
        if (get().activeSessionId === sessionId) {
          useAppStore.getState().showToast(t('chatForksLoadFailed'), 'error');
        }
        return;
      }
      // Staleness guard: a slower fetch for a session the user already left
      // must not seat its forks under the newly opened one.
      if (get().activeSessionId !== sessionId) return;
      set({ forks: Array.isArray(entries) ? entries : [] });
    },

    openBranch(sessionId: string) {
      // The thread ROOT carries the pointer. Switcher targets are always
      // threadmates of the ACTIVE session, so the root resolves from the
      // ACTIVE row first — the target itself may be an unlisted sibling with
      // no row in `sessions`, and reading the target's row first made this
      // PATCH the BRANCH (the guard accepts it — same thread) so the chat
      // list's preview silently stopped following the switch. The target's
      // own row covers the fork flows (they seat the branch before opening);
      // a remembered root covers a switch BETWEEN two unlisted siblings
      // (neither side has a row); a pre-threading row (thread_id null) is
      // its own root.
      const activeId = get().activeSessionId;
      const activeRow = activeId ? get().sessions.find(s => s.session_id === activeId) : undefined;
      const row = get().sessions.find(s => s.session_id === sessionId);
      const rootId =
        activeRow?.thread_id
        || row?.thread_id
        || threadRootByBranch.get(sessionId)
        || (activeId ? threadRootByBranch.get(activeId) : undefined)
        || sessionId;
      apiClient.patch(`/chat/sessions/${rootId}`, { active_branch_id: sessionId })
        .then((updated: ChatSession) => {
          rememberThreadRoot(sessionId, rootId);
          set(s => ({
            sessions: s.sessions.map(ss =>
              ss.session_id === rootId
                ? { ...ss, active_branch_id: updated.active_branch_id }
                : ss,
            ),
          }));
        })
        .catch(() => {
          // The open itself proceeds (it is local); only the list preview
          // diverges — say so instead of failing silently.
          useAppStore.getState().showToast(t('chatBranchSwitchFailed'), 'warning');
        });
      if (get().activeSessionId !== sessionId) {
        get().setActiveSession(sessionId);
      }
    },
  };
}
