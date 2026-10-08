/** Region / pinned-selection actions for the sessions slice, composed into createSessionsSlice.

Extracted from sessions-slice (plan: chat-large-file-decomposition). A focused
helper factory returning the region actions (startAgentChat + unpin + regionLost);
the main slice composes it via object spread over the same (set, get) closure. */
import { useAppStore } from '../../app-store';
import { useUIStore } from '../../ui-store';
import { emit } from '../../../events';
import { t } from '../../../i18n';
import { clearPendingRegion } from '../pending-selection';
import type { ChatState, Set, Get } from '../types';

type RegionActions = Pick<ChatState, 'startAgentChat' | 'unpinRegion' | 'regionLost'>;

export function createRegionActions(set: Set, get: Get): RegionActions {
  return {
    // see SYSTEM: selection-region-agent — attach a pinned region to the ZERO/GHOST chat.
    // The Bot icon (SelectionToolbar) / Cmd+J → agentAction → 'start-agent-chat' event →
    // this action. NO createSession here: the region
    // is held in-memory on the ghost and materialized onto a real row ONLY on first send
    // (ChatInput.handleSend), so clicking "Work with Selection" never creates a chat row.
    async startAgentChat(payload) {
      const app = useAppStore.getState();
      const project = app.currentProject;
      const doc = app.currentDocument;
      if (!project || !doc) return;

      // INVARIANT (access): a pinned-region agent requires full project access. Why: the
      // old click-time createSession enforced this; the ghost redesign removed that POST,
      // so the gate moves HERE — otherwise a Viewer/Commentator would get a working pill +
      // highlight + agent ghost, rejected only on send (violates hide-trigger-AND-guard-
      // handler). agentAction also no-ops for non-full (Cmd+J); backend still enforces.
      if (app.accessLevel !== 'full') {
        app.showToast(t('agentModeRequiresAccess'), 'error');
        return;
      }

      // Decision (locked, plan): ALWAYS target the ghost — a pin starts a fresh zero-chat.
      // Behavior-preserving: startAgentChat already always created a NEW session. If a
      // materialized session is active, drop to the ghost first, then set the region.
      // The materialized row (on send) is an agent session by construction (every AI
      // chat is one); the agent target is derived from the open entity at
      // materialization, as today.
      get().initGhostFromScope();
      set({
        activeSessionId: null,
        messages: [],
        // The slot shows the ACTIVE chat's turn and the ghost has none (the
        // INVARIANT in types.ts; the chat left behind keeps its registration).
        streaming: null,
        ghostRegion: { doc_id: payload.doc_id, relFrom: payload.relFrom, relTo: payload.relTo },
        pendingInputFocus: true,
      });

      // Reveal the chat panel on the owning document.
      useUIStore.getState().setRightPanelTab(doc.document_id, 'chat');
      emit('open-right-panel');
    },

    // Unpin the region: PATCH has_region=false (re-enables auto-apply, drops the
    // containment constraint) + clear the localStorage anchor. The editor highlight
    // hides because it re-derives from the session's has_region flag.
    async unpinRegion(sessionId) {
      await get().updateSession(sessionId, { has_region: false });
      clearPendingRegion(sessionId);
      useAppStore.getState().showToast(t('regionUnpinnedAutoEnabled'), 'info');
    },

    // see SYSTEM: selection-region-agent — the pin's RelativePosition anchor does
    // not resolve against the live ydoc (only a wholesale content replace — checkpoint
    // restore / doc import — destroys it; ordinary edits and the agent's own surgical
    // edits preserve it). A pin over lost anchors is dead weight: it forces confirm
    // and rejects every apply forever, so we auto-unpin and tell the user to re-pin.
    // Idempotent + race-safe: acts only while the session is still pinned, so the
    // editor update-listener, the send path, and the apply path can all call it and
    // only the first wins.
    async regionLost(sessionId) {
      const session = get().sessions.find(s => s.session_id === sessionId);
      if (!session?.has_region) return;
      await get().updateSession(sessionId, { has_region: false });
      clearPendingRegion(sessionId);
      useAppStore.getState().showToast(t('regionLostAfterRestore'), 'warning');
    },
  };
}
