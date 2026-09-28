/** Create action for the sessions slice, composed into createSessionsSlice.

Extracted from sessions-slice (plan: p1-debt-paydown, P1-4). Owns the unified
session-materialization entry point (lazy-create on first send + every create
intent), incl. the access guard, the pending-PATCH flush, the ghost-context warm +
snapshot. The main slice composes it via object spread; cross-action calls go
through `get()`. */
import { apiClient } from '../../../api/client';
import { useAppStore } from '../../app-store';
import { useUIStore } from '../../ui-store';
import { showsBothPanes, refIsScope } from '../../ui-store/documents-slice';
import { t } from '../../../i18n';
import { getLogoutEpoch } from '../../logout-handlers';
import {
  getDerivedGhostContext,
  ghostBaseTargets,
  resetGhostDeltas,
  setContextForSession,
} from '../../../chat/context';
import { fetchDocumentLinksFresh, fetchReferenceLinksFresh, linkCache } from '../../../api/links';
import type { ChatSession } from '../../../types';
import type { ChatState, CreateSessionParams, Set, Get } from '../types';
import { getPendingSessionPatch } from '../inflight';
import { insertAndPinSession } from '../session-helpers';
import { setPendingRegion } from '../pending-selection';

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
      targetDocId,
      hasRegion,
      region,
      reasoningEffort,
    }: CreateSessionParams) {
      // ARCH: unified creator for every entry point (+ button, ChatInput
      // lazy/overflow-create). There is one AI line — every create is an agent chat,
      // so the full-access guard is unconditional and target_doc_id / agent_auto /
      // has_region are forwarded on every create (matching the backend).
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
          has_region: !!hasRegion,
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
      // INVARIANT: the pinned region must be written BEFORE the session becomes active.
      // Why: the highlight's cache gate only re-resolves on a doc change or the
      // regionChanged annotation, and that annotation fires on activation (the Editor
      // effect keyed on activeRegionSessionId) — a region set AFTER activation is never
      // picked up. insertAndPinSession activated
      // the session synchronously above, and the React re-render + annotation dispatch is
      // queued but has NOT run yet (no await since), so this synchronous write lands first.
      if (region) setPendingRegion(session.session_id, region);
      // Ghost-context materialization. Warm the open entity's first-circle into
      // the shared linkCache BEFORE the single derive pass, then snapshot the DERIVED
      // ghost context onto the real session id synchronously, then reset the ghost
      // deltas.
      // ARCH: warm-before-snapshot, so the single getDerivedGhostContext() pass is the
      //   one source of truth for the materialized context — there is deliberately NO
      //   second write path (a post-snapshot backfill through addWithCascade would
      //   union the target's full first-circle back on and clobber whatever the user
      //   unchecked). deriveGhostContext folds base + first-circle, THEN applies
      //   subtractWithCascade on the removed deltas, so an UNCHECKED first-circle child
      //   stays removed. The agent's editable target_doc_id is set on the POST payload
      //   above and is orthogonal to context; every createSession({targetDocId}) caller
      //   passes the OPEN entity, so derive already yields target + first-circle.
      // INVARIANT: skip-if-warm — only fetch ids NOT already in linkCache. The warm  Why: the warm effect usually filled the cache; fetching only missing ids avoids a refetch and keeps the snapshot byte-identical to the picker's selection.
      //   effect (useGhostContextWarm, runs in ChatPanel) usually filled them, so this
      //   avoids a refetch and keeps the snapshot byte-identical to the picker's
      //   displayed selection. Still guarantees first-circle on turn 1 even when the
      //   warm effect never ran (e.g. a region-agent fired from the editor while the
      //   chat tab is closed) — the cold entry is fetched here, no silent
      //   display-vs-send disagreement (no-silent-degradation).
      const warmDocId = useAppStore.getState().currentDocument?.document_id ?? documentId ?? null;
      // Scope projection (panel preview → null): warm exactly what the derived
      // base will fold, matching the bridge's snapshot input.
      const mode = useUIStore.getState().getRefOpenMode(warmDocId ?? '');
      const scopeRefId = refIsScope(mode) ? useAppStore.getState().currentReference : null;
      const warmRefId = scopeRefId?.reference_id ?? referenceId ?? null;
      const warmSplit = !!warmDocId && showsBothPanes(mode);
      const warmTargets = ghostBaseTargets(warmDocId, warmRefId, warmSplit);
      // WHY: this loop looks like a duplicate of useGhostContextWarm's first-circle
      //   fetch (use-ghost-context.ts), but the two intentionally diverge and are NOT
      //   consolidated into a warmFirstCircle(targets) helper:
      //   - targets: base only here (no manual deltas — they are folded by the derive
      //     pass itself), vs base+deltas in the effect.
      //   - no bumpLinkCacheVersion here (this is a one-shot synchronous snapshot, not
      //     a reactive re-derive), vs the effect bumps so the selector recomputes.
      //   - no _pendingLinks dedup (awaited once inline), vs the effect dedupes per key.
      //   - failure is silent best-effort (.catch noop): a failed fetch here only means
      //     the snapshot goes bare-id, matching whatever the picker last showed — NOT the
      //     reactive display path that warrants the effect's warn+toast escalation.
      //   Unifying would force 3+ flags → param-flag branch; left as two explicit loops.
      const warmTasks: Array<Promise<unknown>> = [];
      for (const id of warmTargets.docs) if (!linkCache.has(`doc:${id}`)) warmTasks.push(fetchDocumentLinksFresh(id).catch(() => {}));
      for (const id of warmTargets.refs) if (!linkCache.has(`ref:${id}`)) warmTasks.push(fetchReferenceLinksFresh(id).catch(() => {}));
      if (warmTasks.length) await Promise.all(warmTasks);
      // The warm `await` sits between the POST epoch-check (above) and the
      // snapshot — re-check the epoch so a soft logout that bumped it during the fetch
      // is treated exactly like the post-POST guard (no write to a reset store).
      if (getLogoutEpoch() !== startEpoch) return null;
      const snap = getDerivedGhostContext();
      setContextForSession(session.session_id, snap.docIds, snap.refIds);
      resetGhostDeltas();
      return session;
    },
  };
}
