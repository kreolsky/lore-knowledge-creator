/** Ghost-lifecycle actions for the sessions slice, composed into createSessionsSlice.

Extracted from sessions-slice (plan: chat-large-file-decomposition). A focused
helper factory returning the ghost actions (startGhostChat + override setters +
scope inheritance + region setters); the main slice composes it via object spread.
The actions share the same (set, get) closure the slice passes through. */
import { useUIStore } from '../../ui-store';
import type { ChatState, Set, Get } from '../types';

type GhostActions = Pick<
  ChatState,
  | 'startGhostChat'
  | 'setGhostSystemPrompt'
  | 'resetGhostOverrides'
  | 'initGhostFromScope'
  | 'setGhostRegion'
  | 'clearGhostRegion'
>;

export function createGhostActions(set: Set, get: Get): GhostActions {
  return {
    // SYSTEM: startGhostChat — open a fresh client-only ghost for the open
    // entity. Single entry point for "Add chat" (ChatHeader/ChatInput overflow),
    // the discard/reset button, and any other "new chat" intent. Sync: drops
    // activeSessionId + messages, inherits ghost overrides from the last active AI
    // chat in the project (the project-wide list), and clears the project-level
    // active pointer. The ghost context is DERIVED from the open entity
    // (useGhostChatContext) — there is no stored bucket to clear and nothing to
    // attach here. No POST — materialization is on first send.
    // INVARIANT: never creates a DB row (lazy everywhere — no empty chats).
    // Why: empty chats pollute the session list — the ghost sentinel shows the composer without persistence.
    // WHY: initGhostFromScope reads activeSessionId BEFORE it is cleared
    // Why: a "+" from an active chat inherits model/mode/agent_auto/system_prompt — reading after the clear loses the scope settings.
    // below, so a "+" from an active chat inherits model/mode/agent_auto/system
    // prompt from the chat being left.
    startGhostChat() {
      get().initGhostFromScope();
      // ARCH: focus the composer when a zero/ghost chat opens — every "new chat"
      // entry point (the "+" button, the discard/reset, overflow-new) funnels here,
      // so the cursor lands in the textarea the instant the empty chat appears.
      set({ activeSessionId: null, messages: [], pendingInputFocus: true });
      // WHY: entering the ghost chat clears the project-level active pointer
      // Why: exiting to the zero chat must survive reload/navigation — restoring the pointer would auto-reopen the session the user just left.
      // (lastActiveChatSessionId → null). Why:
      // exiting to the zero chat must survive reload/navigation; restoring the chat
      // the user just left is what they exited FROM.
      useUIStore.getState().setLastActiveChatSession(null);
    },

    setGhostSystemPrompt(id: string | null) {
      set({ ghostSystemPromptId: id });
    },

    resetGhostOverrides() {
      set({ ghostAgentAuto: false, ghostSystemPromptId: null, ghostModel: '', ghostReasoningEffort: null });
    },

    // ARCH: single inheritance resolver.
    // Source = the active session if it is still present in the current project-wide
    // `get().sessions`, else the latest-by-updated_at session; bare defaults when the
    // project holds no AI chats. Sets model/agent_auto/system_prompt_id/reasoning_effort
    // so a fresh ghost visibly matches the last active chat. agent_auto and
    // reasoning_effort are CLIENT-SIDE ONLY — the backend project-latest walk inherits
    // only model + system_prompt_id, so without this a ghost would silently drop them.
    // Reads state BEFORE the caller clears activeSessionId (startGhostChat /
    // openChatWithReference) so the "+" path inherits from the chat being left.
    // ARCH: the donor is the latest AI chat ANYWHERE in the project (the list is
    // project-wide), not just the open branch — frontend and backend inheritance
    // agree on the donor. Only agent_auto is inherited: every AI chat is an agent
    // chat, so there is no mode to inherit.
    initGhostFromScope() {
      const { sessions, activeSessionId } = get();
      const active = activeSessionId
        ? sessions.find(s => s.session_id === activeSessionId) ?? null
        : null;
      const source =
        active ??
        sessions.slice().sort((a, b) =>
          (b.updated_at ?? '').localeCompare(a.updated_at ?? ''),
        )[0] ??
        null;
      if (!source) {
        set({
          ghostAgentAuto: false,
          ghostSystemPromptId: null,
          ghostModel: '',
          ghostReasoningEffort: null,
        });
        return;
      }
      set({
        ghostAgentAuto: !!source.agent_auto,
        ghostSystemPromptId: source.system_prompt_id ?? null,
        ghostModel: source.model ?? '',
        // ARCH: reasoning_effort is inherited
        // CLIENT-SIDE only — the backend latest-of-scope walk covers model +
        // system_prompt_id, so without this a fresh ghost would visibly reset
        // the effort the donor chat ran with (same bridge rationale as agent_auto).
        ghostReasoningEffort: source.reasoning_effort ?? null,
      });
    },

    setGhostRegion(region) {
      set({ ghostRegion: region });
    },

    clearGhostRegion() {
      set({ ghostRegion: null });
    },
  };
}
