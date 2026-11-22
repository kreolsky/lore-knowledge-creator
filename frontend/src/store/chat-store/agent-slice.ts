/** Agent-mode slice — verdicts (mid-turn approval) + setSessionMode/setSessionUIMode. */
// ARCH: SYSTEM: chat-agent-mode-ui — verdict UI + apply-mode switching. The proposal
// actions (applyProposal / applyAllProposals) were deleted with the proposal cluster:
// confirmation is now the mid-turn approval hold, so a mutating call is either applied
// (auto / approved) or rejected.
import type { ChatUIMode } from '../../types';
import { inverseUIMode } from '../../types';
import { apiClient, HttpError } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import type { ChatState, Set, Get } from './types';

type AgentSlice = Pick<
  ChatState,
  | 'setSessionMode'
  | 'setSessionUIMode'
  | 'decideVerdict'
>;

export function createAgentSlice(set: Set, get: Get): AgentSlice {
  return {
    // ─── Mid-turn approval verdicts ───────────────────────────────────────────
    async decideVerdict(
      callId: string,
      action: 'allow_once' | 'allow_session' | 'reject',
      toolName: string,
      reason?: string,
    ) {
      const sessionId = get().activeSessionId;
      if (!sessionId) return 'failed';
      try {
        await apiClient.post('/chat/verdicts', {
          call_id: callId,
          session_id: sessionId,
          action,
          tool_name: toolName,
          reason,
        });
        return 'sent';
      } catch (e) {
        if (e instanceof HttpError && (e.status === 404 || e.status === 409)) {
          // WHY: a 404 here is not a degraded state — the hold is already gone
          // (aborted turn / deleted session), so the card is stale, and folding
          // it IS the correct outcome, not an error to surface.
          // A 409 is the OWNED expired hold: the verdict arrived after
          // HOLD_MAX_S, the call already self-resolved as rejected — fold the
          // card and say WHY (explicit expired state, no silent degradation).
          // Every other failure toasts below.
          if (e.status === 409) {
            useAppStore.getState().showToast(t('verdictExpired'), 'warning');
          }
          return 'stale';
        }
        useAppStore.getState().showToast(t('verdictFailed'), 'error');
        return 'failed';
      }
    },
    setSessionUIMode(mode: ChatUIMode) {
      const sessionId = get().activeSessionId;
      // WHY: inverseUIMode returns the persisted agent_auto bool — the only
      // selector. With one AI line there is no `mode` half, so this toggles
      // confirm <-> auto and nothing else.
      const { agentAuto: wantAuto } = inverseUIMode(mode);
      // INVARIANT (access): agent_auto=true is an auto-apply grant requiring full
      // project access — guard on every path (ghost + session), backend
      // re-enforces. CLAUDE.md: never rely on UI hiding alone.
      if (wantAuto && useAppStore.getState().accessLevel !== 'full') {
        useAppStore.getState().showToast(t('agentModeRequiresAccess'), 'error');
        return;
      }
      // Ghost (no session): hold the choice client-side so the ghost is fully
      // configurable pre-send. No PATCH (no row exists); applied at
      // materialization (createSession reads ghostAgentAuto).
      if (!sessionId) {
        set({ ghostAgentAuto: wantAuto });
        return;
      }
      const current = get().sessions.find(s => s.session_id === sessionId) ?? null;
      if (!current) return;
      // WHY (agent_auto persistence): every transition PATCHes the
      // persisted agent_auto column, exactly like model / system_prompt_id.
      // There is no in-memory uiModeBySession map — the UI mode is DERIVED from
      // the session via deriveUIMode(), so it survives reload.
      set(s => ({
        sessions: s.sessions.map(ss =>
          ss.session_id === sessionId
            ? { ...ss, agent_auto: wantAuto }
            : ss,
        ),
      }));
      void get().updateSession(sessionId, { agent_auto: wantAuto });
    },
    // WHY: setSessionMode collapsed from a
    // (mode, systemPromptId) ask↔agent boundary switch to a single system_prompt
    // PATCH. With one AI line there is no boundary to cross; a named prompt is
    // just framing applied in place. The agent_auto toggle is a separate action
    // (setSessionUIMode).
    async setSessionMode(systemPromptId?: string | null) {
      const sessionId = get().activeSessionId;
      if (!sessionId) return;
      const current = get().sessions.find(s => s.session_id === sessionId) ?? null;
      if (!current) return;
      // Same-session PATCH of system_prompt_id (the only PATCH-able framing field).
      // Omit the call entirely when the value is unchanged.
      if (systemPromptId === undefined || current.system_prompt_id === systemPromptId) return;
      await get().updateSession(sessionId, { system_prompt_id: systemPromptId ?? null });
      // updateSession owns the error toast and leaves the row unchanged on failure.
      const updated = get().sessions.find(s => s.session_id === sessionId);
      if (!updated || (updated.system_prompt_id ?? null) !== (systemPromptId ?? null)) return;
      // WHY: the server re-binds the WHOLE conversation's memory to the new
      // persona (see INVARIANT in backend/chat_sessions/update.py), compaction
      // continuations included, which share no thread_id the client could match —
      // a same-scope re-read (keeps the active chat) replaces the list so every
      // row's origin_persona_id and persona_memory mark follow what the server wrote.
      const projectId = useAppStore.getState().currentProject?.project_id;
      if (projectId) await get().loadSessions(projectId, get().documentId ?? undefined);
    },
  };
}
