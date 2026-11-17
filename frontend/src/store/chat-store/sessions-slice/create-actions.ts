/** Create action for the sessions slice, composed into createSessionsSlice.

Extracted from sessions-slice (plan: p1-debt-paydown, P1-4). Owns the unified
session-materialization entry point (lazy-create on first send + every create
intent), incl. the access guard and the pending-PATCH flush. The main slice composes it via object spread; cross-action calls go
through `get()`. */
import { apiClient } from '../../../api/client';
import { useAppStore } from '../../app-store';
import { useUIStore } from '../../ui-store';
import { refIsScope } from '../../ui-store/documents-slice';
import { t } from '../../../i18n';
import { getLogoutEpoch } from '../../logout-handlers';
import type { ChatSession } from '../../../types';
import type { ChatState, CreateSessionParams, Set, Get } from '../types';
import { getPendingSessionPatch } from '../inflight';
import { insertAndPinSession } from '../session-helpers';

type CreateActions = Pick<ChatState, 'createSession'>;

export function createCreateActions(set: Set, _get: Get): CreateActions {
  return {
    async createSession({
      projectId,
      documentId,
      model,
      systemPromptId,
      referenceId,
      parentSessionId,
      focus,
      agentAuto,
      unindexed,
      targetDocId,
      reasoningEffort,
    }: CreateSessionParams) {
      // ARCH: unified creator for every entry point (+ button, ChatInput
      // lazy/overflow-create). There is one AI line — every create is an agent chat,
      // so the full-access guard is unconditional and target_doc_id / agent_auto
      // are forwarded on every create (matching the backend).
      // INVARIANT (access): every AI chat requires full project access. This is
      // the single unified entry point, so the guard lives here. Why: CLAUDE.md —
      // enforce on the backend AND guard every handler with an early return;
      // never rely on UI hiding alone. Backend still enforces; this is defense.
      if (useAppStore.getState().accessLevel !== 'full') {
        useAppStore.getState().showToast(t('agentModeRequiresAccess'), 'error');
        return null;
      }
      // WHY: flush any pending updateSession PATCH before the create POST so  Why: flushing first ensures the inheritance resolver sees the latest model/system_prompt; createSession is the single materialization entry, so this is the only place it matters.
      // the backend inheritance resolver sees the latest model / system_prompt
      // values. createSession is the SINGLE materialization entry point (ChatInput
      // lazy-create on first send), so the guarantee lives here. Why: the
      // model/system_prompt carried by the in-flight PATCH must land server-side
      // before inheritance walks the latest-of-scope row.
      // Capture the logout generation before any await — a soft logout during the
      // flush/POST resets the store, and the resolving POST must not insert the prior
      // user's freshly-created session back into it.
      const startEpoch = getLogoutEpoch();
      const pending = getPendingSessionPatch();
      if (pending) await pending;
      let session: ChatSession;
      try {
        // ARCH: Omit inheritance fields entirely when caller passes undefined — backend resolver
        // then walks Reference → parent Document → defaults. Explicit null means "use default"
        // (no prompt). Forward null when systemPromptId !== undefined so the backend
        // can distinguish explicit-default from omitted-inherit.
        // parent_session_id (when set) overrides the latest-of-scope walk: a
        // subsequent chat inherits from the chat where new-chat was clicked, not from
        // whatever chat happens to be newest in the scope.
        session = await apiClient.post('/chat/sessions', {
          project_id: projectId,
          ...(documentId ? { document_id: documentId } : {}),
          ...(referenceId ? { reference_id: referenceId } : {}),
          ...(model ? { model } : {}),
          ...(systemPromptId !== undefined ? { system_prompt_id: systemPromptId } : {}),
          ...(parentSessionId ? { parent_session_id: parentSessionId } : {}),
          // Agent fields forwarded unconditionally (every AI chat is agent).
          // target_doc_id defaults to the open entity when the caller omits it —
          // through the scope projection: in panel quick preview the open entity
          // is the DOCUMENT (the previewed ref is not the scope).
          target_doc_id: targetDocId ?? (() => {
            const s = useAppStore.getState();
            const mode = useUIStore.getState().getRefOpenMode(s.currentDocument?.document_id ?? '');
            return (refIsScope(mode) ? s.currentReference?.reference_id : undefined) ?? s.currentDocument?.document_id;
          })(),
          agent_auto: !!agentAuto,
          unindexed: !!unindexed,
          // ARCH: present ONLY when the ghost
          // carried an explicit effort — omitted means Default (column absent).
          // Create trusts the caller like `model` does; the PATCH guard is the
          // validation point.
          ...(reasoningEffort ? { reasoning_effort: reasoningEffort } : {}),
        });
      } catch {
        // createSession surfaces the failure itself (single place, honors "no silent
        // degradation") so delegating callers need no POST catch of their own.
        useAppStore.getState().showToast(t('agentModeStartFailed'), 'error');
        return null;
      }
      // If a soft logout bumped the epoch while the POST was in flight, the
      // store has been reset — do NOT insert the prior user's session (the row exists
      // server-side under their user_id; the next user's loadSessions never returns it).
      // Returns null (the established no-op signal) so ChatInput's lazy-create no-ops.
      if (getLogoutEpoch() !== startEpoch) return null;
      insertAndPinSession(set, session, {
        focus: focus !== false,
      });
      return session;
    },
  };
}
