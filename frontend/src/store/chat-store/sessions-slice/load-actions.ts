/** Load action for the sessions slice, composed into createSessionsSlice.

Extracted from sessions-slice (plan: p1-debt-paydown, P1-4). Owns the
project-scoped session-list load + the SWR pre-gate paint + the active-session
resolver flow, plus the shared sessions SWR cache (self-registered into the logout
registry). The main slice composes it via object spread; cross-action calls go
through `get()`. */
import { apiClient } from '../../../api/client';
import { useUIStore } from '../../ui-store';
import { useAppStore } from '../../app-store';
import { resolveActiveSession } from '../../chat-session-resolver';
import { hydrateFromSessions } from '../../../chat/context';
import { t } from '../../../i18n';
import { createResourceCache } from '../../../api/swr-cache';
import { registerLogoutHandler, getLogoutEpoch } from '../../logout-handlers';
import { stripFrames } from '../conversation-feed';
import { seatSessionRows, afterSessionRowsCommitted } from '../session-rows';
import type { ChatSession, ChatMessage } from '../../../types';
import type { ChatState, Set, Get } from '../types';
import { loadInFlight } from '../inflight';

// ARCH: SWR cache for the AI-chat session list, keyed by the same scopeKey the
// in-flight map uses (see SYSTEM: swr-cache). A re-open of a previously seen scope
// paints the dropdown instantly (stale) while loadSessions still fetches + runs the
// resolver against the fresh list. INVARIANT: the cached paint is a PRE-gate paint,
// NOT a terminal — chatScopeLoading is untouched here and cleared exactly as before.  Why: the cached sessions paint the dropdown instantly, but the scope gate is untouched — it still clears only at the real terminal, so a stale cache can't pose as 'loaded'.
const sessionsCache = createResourceCache<ChatSession[]>();

/** Test-only: reset the AI-chat sessions SWR cache. */
export function clearSessionsCache(): void {
  sessionsCache.clear();
}

// Self-register the chat sessions cache clear into the logout registry so
// setCurrentUser(null) drops it. chat-store imports app-store, so app-store can't
// import this back to clear it directly — this bridge mirrors registerAppBridge.
registerLogoutHandler(clearSessionsCache);

type LoadActions = Pick<ChatState, 'loadSessions'>;

export function createLoadActions(set: Set, get: Get): LoadActions {
  return {
    async loadSessions(projectId: string, documentId?: string) {
      // ARCH: the active chat is PROJECT-SCOPED.
      // loadSessions fetches ONE list per project; switching documents within a
      // project does NOT reload (ChatPanel calls this on PROJECT change only).
      // The open document is still passed to the backend (it is the access gate),
      // but the active-chat pick comes from the project-level lastActiveChatSessionId.
      // scopeKey is the PROJECT: the in-flight dedup + SWR cache identity is
      // per-project ("load once per project"), not per-document.
      const scopeKey = projectId;

      // ARCH: Promise-cached dedup — two callers requesting the same project await
      // the same in-flight promise. No caller can proceed past a still-in-flight hydrate.
      const inflight = loadInFlight.get(scopeKey);
      if (inflight) return inflight;

      const currentActiveId = get().activeSessionId;
      // A scope change now means a PROJECT change (within a project loadSessions is
      // never called with a different document). It gates: the loading spinner
      // (covers the panel so the prior project's chat does not flash) + the ghost
      // region reset. The active chat / messages are NOT cleared synchronously — the
      // resolver restores the project's last-active chat or yields a ghost.
      const scopeChanged = get().documentId !== (documentId ?? null);
      // Set the requested scope synchronously BEFORE any await so the store holds an
      // authoritative "latest requested scope" the post-fetch liveness guard compares
      // against. Every loadSessions call overwrites documentId here.
      set({
        documentId: documentId ?? null,
        // WHY: raise the spinner only on a real scope (project) change. Why:
        // the flag drives a single MessageList spinner that hides any prior chat
        // during the load — without it the previous project's chat would flash. On a
        // same-scope re-fetch (openChatWithReference) keep_current holds the active
        // chat, so a spinner would blank a chat that never goes away.
        ...(scopeChanged ? { chatScopeLoading: true, ghostRegion: null } : {}),
      });

      // SWR pre-gate paint: on a scope (project) change, show the last-seen session
      // list for this project instantly under the spinner. NOT a terminal —
      // chatScopeLoading stays true; the resolver runs below against the fresh list.
      if (scopeChanged) {
        const cachedSessions = sessionsCache.get(scopeKey);
        if (cachedSessions) set({ sessions: cachedSessions });
      }

      const doLoad = (async () => {
        // lastActiveId: the project-level active chat (ui-store
        // lastActiveChatSessionId). The resolver's restore source.
        const lastActiveId = useUIStore.getState().getLastActiveChatSession();
        // Capture the logout generation BEFORE the await: a soft logout while this
        // fetch is in flight bumps the epoch (clearUserScopedCaches) and resets the
        // store — the resolving (prior user's) result must not be written back into the
        // cache/store afterward.
        const startEpoch = getLogoutEpoch();
        try {
          const targetDocId = documentId || '';
          // Opt into the sessions+messages piggyback and hint the session we expect to
          // activate (the project-level last-active id) so the panel can open in ONE
          // round-trip. The backend attaches active_messages only for a VALID
          // preferred id; otherwise it is NULL and we do a normal second loadMessages
          // via the resolver's pick.
          const params = new URLSearchParams({
            project_id: projectId,
            document_id: targetDocId,
            with_active_messages: 'true',
          });
          if (lastActiveId) params.set('preferred_session_id', lastActiveId);
          type SessionsResponse =
            | ChatSession[]
            | { sessions: ChatSession[]; active_messages: { session_id: string; messages: ChatMessage[] } | null };
          const resp: SessionsResponse = await apiClient.get(`/chat/sessions?${params}`);
          // Tolerate both shapes: the object when with_active_messages piggybacks
          // messages, and the plain list the endpoint still returns without the flag.
          const rawSessions = Array.isArray(resp) ? resp : resp.sessions;
          const activeMessages = Array.isArray(resp) ? null : resp.active_messages;
          // RACE GUARD: if a newer scope request overwrote documentId while this load
          // was fetching, discard the result. Applying a stale project's
          // setActiveSession would pin the old project's chat onto the new one.
          // Why store values, not closure values: only the store holds the LATEST
          // request — every loadSessions overwrites documentId synchronously at entry,
          // so a newer request is visible here immediately.
          // Also abort if a soft logout bumped the epoch mid-flight — the reset already
          // cleared the store, a stale resolve must not re-populate it.
          if (
            get().documentId !== (documentId ?? null) ||
            getLogoutEpoch() !== startEpoch
          ) {
            return;
          }
          // `mode` is not on the wire (the backend serializer drops the column) —
          // sessions are stored as-is and the apply-mode is derived from agent_auto
          // via deriveUIMode.
          sessionsCache.seed(scopeKey, rawSessions);
          set({ sessions: rawSessions });

          // WHY: hydrate per-session UI context from authoritative DB values
          // so a fresh device/browser sees the same context the chat was saved with.  Why: context is hydrated from DB values, not local state, so a fresh device sees the saved context (no device drift).
          // WHY: hydrateFromSessions requires app-store references to be loaded
          // first; otherwise ref ids misclassify as doc ids. Why: the internal split
          // is by the reference id set from app-store. If references are not yet
          // loaded here, hydrateFromSessions emits a console warning (observability).
          hydrateFromSessions(rawSessions);

          // ARCH: All lifecycle logic delegated to the pure resolver. The resolver
          // restores the project's last-active chat (matched against the whole
          // project-wide list) or yields `none` → a client-only ghost
          // (activeSessionId = null). Materialization of a real DB row happens ONLY
          // on the first sent message (ChatInput lazy-create). No auto-create on load.
          // INVARIANT: clear chatScopeLoading at every terminal. restore_saved
          // funnels through setActiveSession → loadMessages, which clears the gate
          // when its fetch settles. keep_current / none must clear here — they never  Why: every load branch must clear the scope gate; restore_saved clears via loadMessages, but keep_current/none reach no loadMessages, so they clear here.
          // reach loadMessages.
          const resolution = resolveActiveSession({
            sessions: rawSessions,
            currentActiveId,
            lastActiveSessionId: lastActiveId,
          });
          switch (resolution.action) {
            case 'keep_current':
              set({ chatScopeLoading: false });
              return;
            case 'none':
              // Ghost render state: no active session, no messages, loading gate
              // cleared. The composer / context picker / mode toggle render against
              // null (ghost) and materialize a row on first send. The ghost context
              // is DERIVED from the open entity (useGhostChatContext +
              // useGhostContextWarm in ChatPanel) — nothing to attach here.
              // streaming: null — the slot shows the ACTIVE chat's turn and a
              // ghost has none (the INVARIANT in types.ts).
              get().initGhostFromScope();
              set({ activeSessionId: null, messages: [], streaming: null, chatScopeLoading: false, pendingInputFocus: true });
              return;
            case 'restore_saved':
              // When the backend piggybacked the very session we resolved,
              // commit its messages directly and skip loadMessages (one round-trip).
              // Mismatch (preferred id absent / stale → active_messages NULL) → normal
              // fetch path via setActiveSession → loadMessages.
              if (activeMessages && activeMessages.session_id === resolution.sessionId) {
                // INVARIANT (timeline parity): the piggybacked rows go through the
                // SAME assembler feed loadMessages runs — the strip only builds
                // the store rows. Why: the piggyback carries `frames` for
                // assistant rows, and committing them raw left the frames
                // unassembled — the restored session rendered text-only until a
                // manual re-select, the live-vs-reload divergence the read path
                // exists to prevent.
                // ORDER (timeline ownership): activate FIRST — the chat-store
                // ownership watcher clears the PREVIOUS chat's published
                // timeline on the activeSessionId transition — THEN
                // seatSessionRows publishes the fresh window. Both are
                // synchronous in one tick, so no render can land between them;
                // the reverse order would let the watcher wipe the
                // just-published window (pinned in stale-timeline-clear.test.ts).
                const restored = activeMessages.messages.map(stripFrames);
                get().setActiveSession(resolution.sessionId, restored);
                // This branch skips loadMessages, so it runs loadMessages's
                // two load steps itself (see the INVARIANT in session-rows.ts).
                seatSessionRows(get, set, resolution.sessionId, activeMessages.messages);
                afterSessionRowsCommitted(get, resolution.sessionId, restored.length);
              } else {
                get().setActiveSession(resolution.sessionId);
              }
              return;
          }
        } catch {
          loadInFlight.delete(scopeKey);
          // INVARIANT: clear the gate only when THIS request still owns the scope (same
          // liveness check as the race-guard above). Why: a superseded loadSessions
          // rejecting must not blank the spinner a newer scope is driving.
          if (get().documentId === (documentId ?? null)) {
            set({ chatScopeLoading: false });
          }
          useAppStore.getState().showToast(t('failedToLoadChatSessions'), 'error');
        }
      })();

      loadInFlight.set(scopeKey, doLoad);
      // INVARIANT: Delete the cache entry once settled. Concurrent callers in the
      // same tick still dedupe via the in-flight Map lookup above, but a later visit
      // to the same project MUST re-fetch. Why: otherwise project A → B → A hits a
      // resolved promise and returns instantly, leaving sessions / activeSessionId
      // stuck on B until page reload.
      doLoad.finally(() => {
        if (loadInFlight.get(scopeKey) === doLoad) loadInFlight.delete(scopeKey);
      });
      return doLoad;
    },
  };
}
