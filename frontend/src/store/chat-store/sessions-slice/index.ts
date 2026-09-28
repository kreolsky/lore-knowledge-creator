/** Sessions slice — load, create, update, delete, setActive, openChatWithReference.

Holds the session CRUD + load + ref-open flow; the ghost-lifecycle, region/
pinned-selection, load, and create actions live in composed helper factories
(`./ghost-actions`, `./region-actions`, `./load-actions`, `./create-actions`) and
are mixed in via spread. The slice factory + the smaller actions
(openChatWithReference, setActiveSession, updateSession, deleteSession) live here.

Split from the flat sessions-slice.ts (plan: p1-debt-paydown, P1-4) following the
slice rule: one Zustand slice > ~200 LOC → <slice>/ folder; flat <slice>.ts only
while small. Re-exports createSessionsSlice + clearSessionsCache so the
chat-store.ts aggregator + tests keep their `./sessions-slice` import path. */
import type { Reference, ChatSession, ChatMessage } from '../../../types';
import { apiClient } from '../../../api/client';
import { useAppStore } from '../../app-store';
import { useUIStore } from '../../ui-store';
import { emit } from '../../../events';
import { t } from '../../../i18n';
import { clearContextForSession } from '../../../chat/context';
import { markChatScopeLoaded } from '../../../chat/scope-tracker';
import type { ChatState, Set, Get } from '../types';
import {
  getPendingSessionPatch,
  setPendingSessionPatch,
} from '../inflight';

import { createGhostActions } from './ghost-actions';
import { createRegionActions } from './region-actions';
import { createLoadActions } from './load-actions';
import { createCreateActions } from './create-actions';

export { clearSessionsCache } from './load-actions';

type SessionsSlice = Pick<
  ChatState,
  | 'loadSessions'
  | 'openChatWithReference'
  | 'createSession'
  | 'setActiveSession'
  | 'updateSession'
  | 'deleteSession'
  | 'startGhostChat'
  | 'setGhostSystemPrompt'
  | 'resetGhostOverrides'
  | 'initGhostFromScope'
  | 'startAgentChat'
  | 'unpinRegion'
  | 'regionLost'
  | 'setGhostRegion'
  | 'clearGhostRegion'
>;

export function createSessionsSlice(set: Set, get: Get): SessionsSlice {
  return {
    ...createGhostActions(set, get),
    ...createRegionActions(set, get),
    ...createLoadActions(set, get),
    ...createCreateActions(set, get),

    // ARCH: Composite "Chat with Reference" action. Owns the sequence end-to-end:
    //   1. Open the ref (hydrateReference) — commits it as currentReference, which
    //      IS the derive base, so the ghost context includes the ref UNCONDITIONALLY
    //      (no intent flag needed — the old write-only setTalkToDocument is gone).
    //   2. Load the project-wide session list (doc-scope == project-wide) so
    //      initGhostFromScope has a donor + the empty-screen list is populated.
    //   3. ALWAYS land on a ref-scoped ghost (activeSessionId = null) with the ref
    //      in context — never auto-restores an existing session.
    //   4. Reveal chat panel.
    //
    // WHY: "Chat with Reference" ALWAYS opens the null/ghost chat with the
    // reference in context — it NEVER auto-restores an existing session. Why: the
    // user decides whether to start fresh (send a message → materializes a new row)
    // or pick an old chat from the empty-screen list. A deterministic ghost whose
    // DERIVED context includes the ref is the only path that reliably shows the
    // ref in the content picker and persists it on the first send.
    async openChatWithReference(ref: Reference) {
      const app = useAppStore.getState();
      const project = app.currentProject;
      const doc = app.currentDocument;
      if (!project || !doc) return;

      const ui = useUIStore.getState();

      // Panel quick preview cannot host this flow: the ref must BE the scope for
      // the ghost to derive ref-only context (the flow's contract). Exit preview
      // first — in 'center' a lone open ref IS the derive base, and the refs-tab
      // reveal effect stands down (mode !== 'panel').
      if (ui.getRefOpenMode(doc.document_id) === 'panel') {
        ui.setRefOpenMode(doc.document_id, 'center');
      }

      // Step 1: Open the ref. hydrateReference commits the list ref as
      // currentReference immediately (metadata-only LIST → loading state) and fills
      // the body async (fire-and-forget so the chat panel opens without waiting on
      // the GET). currentReference IS the derive base, so the ghost context
      // includes the ref unconditionally — no separate intent flag is needed.
      void app.hydrateReference(ref);

      try {
        // Step 2: Load the project-wide list (doc-scope) so initGhostFromScope has
        // a donor + the empty-screen list is populated. loadSessions' resolver MAY
        // restore a session; we override it to a ghost next. loadMessages'
        // staleness guard (returns when activeSessionId no longer matches) protects
        // against an in-flight restored-session message fetch landing in the ghost.
        await get().loadSessions(project.project_id, doc.document_id);
      } catch {
        app.showToast('Failed to open chat with reference', 'error');
        return;
      }

      // Step 3: ALWAYS a ref-scoped ghost. Inherit overrides from the loaded
      // project-wide sessions (the latest AI chat anywhere in the project) + force
      // activeSessionId = null. The ghost context is DERIVED from the open
      // reference (useGhostChatContext reads currentReference) — nothing is attached
      // here; the derived value is snapshotted onto the materialized row on the
      // first send.
      // WHY: initGhostFromScope runs BEFORE activeSessionId is cleared.
      // Why: if the resolver restored a session, the ghost must inherit from it
      // rather than fall back to bare defaults.
      get().initGhostFromScope();
      set({ activeSessionId: null, messages: [] });

      // Mark the project's chat scope as loaded so the ChatPanel mount effect
      // (triggered by setRightPanelTab below) does NOT re-run loadSessions. That
      // second run would hit the resolver with currentActiveId = null and restore
      // the project's last-active chat, overwriting the null/ghost we just set.
      markChatScopeLoaded(project.project_id);

      // Step 4: Reveal chat panel.
      ui.setRightPanelTab(doc.document_id, 'chat');
      emit('open-right-panel');
    },

    setActiveSession(sessionId: string | null, preloadedMessages?: ChatMessage[]) {
      set({
        activeSessionId: sessionId,
        // Commit piggybacked messages immediately when provided, so the panel paints
        // without the second round-trip. Absent → [] (loadMessages fills it).
        messages: preloadedMessages ?? [],
        selectedSiblings: {},
        streaming: null,
        imageGen: {},
      });
      const session = sessionId ? get().sessions.find(s => s.session_id === sessionId) : null;
      if (session) {
        // WHY: pin this session as the PROJECT-LEVEL active chat — this is the
        // restore source (the last chat active in this project). Why: it must survive
        // document navigation and reload, so it cannot be document-scoped.
        useUIStore.getState().setLastActiveChatSession(sessionId);
      }
      if (!sessionId) return;
      if (preloadedMessages) {
        // INVARIANT: the piggyback path must clear chatScopeLoading itself — it skips
        // loadMessages, which is the terminal that normally clears the gate for the
        // restore_saved branch. Why: nothing else clears it on this path, so the
        // scope spinner would stay up forever.
        set({ chatScopeLoading: false, messagesLoading: false });
        return;
      }
      get().loadMessages(sessionId);
    },

    async updateSession(sessionId: string, data: { title?: string; model?: string; system_prompt_id?: string | null; context_ids?: string[]; mode?: string; agent_auto?: boolean; has_region?: boolean; reasoning_effort?: string | null; document_id?: string | null }) {
      const p = apiClient.patch(`/chat/sessions/${sessionId}`, data)
        .then((updated: ChatSession) => {
          set(s => ({
            // WHY: update_session returns
            // last_message_at=null (it doesn't carry the last-activity map), so
            // preserve the in-memory value rather than discarding it — otherwise
            // a settings edit (which bumps updated_at server-side) would revert
            // the card to the patch time and re-sort the chat to the top by patch
            // time until the next list_sessions reload.
            sessions: s.sessions.map(ss => ss.session_id === sessionId
              ? { ...updated, last_message_at: updated.last_message_at ?? ss.last_message_at }
              : ss),
          }));
        })
        .catch(() => {
          useAppStore.getState().showToast('Failed to update session', 'error');
        });
      setPendingSessionPatch(p);
      try { await p; } finally {
        if (getPendingSessionPatch() === p) setPendingSessionPatch(null);
      }
    },

    async deleteSession(sessionId: string) {
      try {
        await apiClient.delete(`/chat/sessions/${sessionId}`);
      } catch {
        useAppStore.getState().showToast(t('failedToDeleteChatSession'), 'error');
        return;
      }
      clearContextForSession(sessionId);
      const wasActive = get().activeSessionId === sessionId;
      if (wasActive) {
        get().initGhostFromScope();
        // Clear the project-level active pointer so a reload does not try to
        // restore the just-deleted chat.
        useUIStore.getState().setLastActiveChatSession(null);
      }
      set(s => {
        const sessions = s.sessions.filter(ss => ss.session_id !== sessionId);
        return {
          sessions,
          activeSessionId: wasActive ? null : s.activeSessionId,
          messages: wasActive ? [] : s.messages,
        };
      });

      if (!wasActive) return;
      // The deleted chat was active → land on a ghost; its context is DERIVED from
      // the open entity (no attach needed).
    },
  };
}
