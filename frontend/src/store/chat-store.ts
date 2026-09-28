/** Zustand store for AI Chat — sessions, message tree, streaming state. */
// ARCH: Separate store from app-store — chat has complex tree state (parent_id, siblings, forks)
// fully isolated from collab/editor. Only link: rightPanelTab === 'chat' in app-store.
// ARCH: Chat persistence goes through ui-store's PersistedUIState → server.
// chat-store reads/writes the project-level active chat via ui-store
// setLastActiveChatSession / getLastActiveChatSession.
// SYSTEM: chat-store — isolated Zustand store for AI chat (sessions, message tree, streaming)

import { create } from 'zustand';
import { useAppStore } from './app-store';
import { useUIStore } from './ui-store';
import { showsBothPanes, refIsScope } from './ui-store/documents-slice';
import { setupChatContextBridge, hydrateFromSessions } from '../chat/context';
import type { ChatState } from './chat-store/types';
import { createSessionsSlice } from './chat-store/sessions-slice';
import { createMessagesSlice } from './chat-store/messages-slice';
import { createAgentSlice } from './chat-store/agent-slice';
import { createMiscSlice } from './chat-store/misc-slice';
import { registerLogoutHandler } from './logout-handlers';
import { publishTimelineReset } from './chat-store/conversation-feed';

export { selectActivePath } from './chat-store/tree';

// ARCH: context.ts PATCH response updates chat-store.sessions via bridge callback.
// Module-level init to avoid circular dependency (context.ts does NOT import chat-store).
setupChatContextBridge({
  updateChatSession: (sessionId, updated) => {
    useChatStore.setState(s => ({
      sessions: s.sessions.map(ss => ss.session_id === sessionId ? updated : ss),
    }));
  },
  showToast: (msg, type) => useAppStore.getState().showToast(msg, type),
  getActiveSessionId: () => useChatStore.getState().activeSessionId,
  getAppStoreState: () => {
    const s = useAppStore.getState();
    // Panel quick preview: the ghost materialization snapshot (getDerivedGhostContext)
    // derives from the SCOPE reference — a previewed ref arrives as null so the
    // materialized session's context is the document's. One projection, applied at
    // the bridge seam so context.ts stays free of app/ui imports.
    const mode = useUIStore.getState().getRefOpenMode(s.currentDocument?.document_id ?? '');
    return { currentDocument: s.currentDocument, currentReference: refIsScope(mode) ? s.currentReference : null };
  },
  getSplitView: (docId: string) => showsBothPanes(useUIStore.getState().getRefOpenMode(docId)),
  getReferenceIdSet: () => new Set(useAppStore.getState().references.map(r => r.reference_id)),
});

export const useChatStore = create<ChatState>((set, get) => ({
  // — initial state —
  sessions: [],
  activeSessionId: null,
  documentId: null,

  messages: [],
  messagesLoading: false,
  messagesError: false,
  chatScopeLoading: false,
  selectedSiblings: {},

  streaming: null,
  // The dsh assembler's published timeline (SYSTEM: dsh-conversation) — see
  // ChatState.conversation. Empty until a session's window is fed.
  conversation: [],
  turnRanges: {},
  turnStartSeq: null,
  // Detached generate_image
  // phases, keyed by runId (outlive the turn; independent concurrent runs). See
  // ChatState.imageGen.
  imageGen: {},

  models: [],
  visionModels: [],
  // Per-model real context window (gateway context_length); populated by loadModels.
  // {} until /models resolves (effectiveCap falls back).
  contextWindows: {},
  // Per-model reasoning capability (plan reasoning-effort-selector); {} until
  // /models resolves → no effort dropdown.
  reasoning: {},
  modelsLoaded: false,
  defaultModel: '',
  // maxAttachmentMb lives in app-store now (single-source, L1).
  // WHY default false: pessimistic until loadModels confirms the agent service is up
  // — keeps the Agent option disabled until a positive signal arrives.
  agentAvailable: false,
  agentUnavailableReason: null,

  pendingInputFocus: false,
  pendingImages: [],
  // SYSTEM: chat-draft — composer text held in-store (survives tab-switch unmount).
  draft: '',
  listFilter: null,

  // Ghost overrides: defaults match createSession's no-opts path.
  // There is no ghost mode: every AI chat is an agent chat.
  ghostAgentAuto: false,
  ghostSystemPromptId: null,
  ghostModel: '',
  ghostReasoningEffort: null,
  ghostRegion: null,

  // — slices —
  ...createSessionsSlice(set, get),
  ...createMessagesSlice(set, get),
  ...createAgentSlice(set, get),
  ...createMiscSlice(set, get),
}));

// One-shot cleanup: the pinned-selection feature was removed; clear any
// residual localStorage it wrote so no client-side trace remains.
try {
  localStorage.removeItem('lore.agent.pendingSelectionBySession.v1');
} catch (e) {
  // WHY: log only — removing a dead key is housekeeping; nothing the user sees depends on it.
  console.debug('pin-scope cleanup skipped', e);
}

// Reset the whole chat store on soft logout so the prior user's
// sessions/messages/activeSessionId can't render after a same-tab re-login. The SWR
// cache clear (sessions-slice) only drops the cache — it does NOT touch the store
// fields that the UI actually renders, which survive logout (module-level Zustand
// state). reset() also aborts any in-flight stream + clears inflight/tree caches.
// Registered here (not in a slice) because it needs the assembled useChatStore.
registerLogoutHandler(() => useChatStore.getState().reset());

// ARCH (context-race): loadSessions hydrates per-session context, but on F5 the
// app-store reference set may still be empty when hydrateFromSessions first runs,
// so reference ids misclassify into documentIds. Re-hydrate whenever references
// load so the doc/ref split is corrected.
// Why: hydrateFromSessions is idempotent — it skips sessions with in-flight
// PATCHes and merges server values (never clobbers optimistic state) — so
// re-running it on a reference-set change is safe.
useAppStore.subscribe((state, prevState) => {
  if (state.references === prevState.references) return;
  const sessions = useChatStore.getState().sessions;
  if (sessions.length > 0) hydrateFromSessions(sessions);
});

// INVARIANT(corruption): timeline ownership — the published timeline belongs
// to the ACTIVE session (SYSTEM: dsh-conversation) and any activeSessionId
// transition republishes it EMPTY. Why: MessageList owns every node above
// turnStartSeq to the streaming message, so a stale publication plus a fresh
// -Infinity boundary (beginTurn on the new chat's first send) paints the
// PREVIOUS chat's timeline inside the new chat — stale content posing as
// current. ONE watcher covers every session-change site — ghost exits,
// delete, materialization, setActiveSession, the resolver branches — and
// every future one; per-site wiring is what rotted (clearFeed had zero
// production callers). Clears the STORE half only (publishTimelineReset):
// the module engine is left alone — exiting mid-stream must not drop the old
// chat's engine, whose still-arriving frames keep appending to it; the
// engine switches lazily at the next ensureEngine, exactly as before.
// Ordering contract: a site that republishes a window on the SAME transition
// (the piggyback restore in load-actions.ts) must activate FIRST and publish
// SECOND — both synchronous in one tick, so no render lands between them
// (pinned by stale-timeline-clear.test.ts).
useChatStore.subscribe((state, prevState) => {
  if (state.activeSessionId === prevState.activeSessionId) return;
  publishTimelineReset(patch => useChatStore.setState(patch));
});
