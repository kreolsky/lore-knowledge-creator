/** Zustand store for AI Chat — sessions, message tree, streaming state. */
// ARCH: Separate store from app-store — chat has complex tree state (parent_id, siblings, forks)
// fully isolated from collab/editor. Only link: rightPanelTab === 'chat' in app-store.
// ARCH: Chat persistence goes through ui-store's PersistedUIState → server.
// chat-store reads/writes the project-level active chat via ui-store
// setLastActiveChatSession / getLastActiveChatSession.
// SYSTEM: chat-store — isolated Zustand store for AI chat (sessions, message tree, streaming)

import type { AttachSurface } from '../chat/attach-image';
import { create } from 'zustand';
import type { ChatState } from './chat-store/types';
import { createSessionsSlice } from './chat-store/sessions-slice';
import { createMessagesSlice } from './chat-store/messages-slice';
import { createBranchesSlice } from './chat-store/branches-slice';
import { createInboxSlice, resetInbox } from './chat-store/inbox-slice';
import { createAgentSlice } from './chat-store/agent-slice';
import { createMiscSlice } from './chat-store/misc-slice';
import { createFilesSlice } from './chat-store/files-slice';
import { registerLogoutHandler } from './logout-handlers';
import { publishTimelineReset } from './chat-store/conversation-feed';

export { selectBranchPath } from './chat-store/tree';

export const useChatStore = create<ChatState>((set, get) => ({
  // — initial state —
  sessions: [],
  activeSessionId: null,
  documentId: null,

  messages: [],
  messagesLoading: false,
  messagesError: false,
  chatScopeLoading: false,
  // The ACTIVE session's switcher projection (branches-slice); cleared on
  // every session switch and reset.
  forks: [],
  pendingScroll: null,

  streaming: null,
  // The dsh assembler's published timeline (SYSTEM: dsh-conversation) — see
  // ChatState.conversation. Empty until a session's window is fed.
  conversation: [],
  turnRanges: {},
  turnStartSeq: null,
  todos: null,

  models: [],
  visionModels: [],
  toolModels: [],
  structuredOutputModels: [],
  // Per-model real context window (gateway context_length); populated by loadModels.
  // {} until /models resolves (effectiveCap falls back).
  contextWindows: {},
  // The instance's context-length nudge dial (GET /chat/models `context_nudge`);
  // null until /models resolves → no banner.
  contextNudge: null,
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
  modelPickerOpen: false,
  pendingImages: [],
  pendingImageMeta: {},
  pressingImages: 0,
  pendingFiles: [],
  // SYSTEM: chat-draft — composer text held in-store (survives tab-switch unmount).
  draft: '',

  // The active chat's dsh inbox + status (inbox-slice).
  inboxQueue: [],
  dshRunning: false,
  // Per-session working set (the chat list's running mark) — inbox-slice.ts.
  runningSessions: new Set<string>(),
  liveSend: null,

  // Ghost overrides: defaults match createSession's no-opts path.
  // There is no ghost mode: every AI chat is an agent chat.
  ghostAgentAuto: false,
  ghostUnindexed: false,
  ghostSystemPromptId: null,
  ghostModel: '',
  ghostReasoningEffort: null,

  // — slices —
  ...createSessionsSlice(set, get),
  ...createMessagesSlice(set, get),
  ...createBranchesSlice(set, get),
  ...createInboxSlice(set, get),
  ...createAgentSlice(set, get),
  ...createMiscSlice(set, get),
  ...createFilesSlice(set, get),
}));

// One-shot cleanup: the pinned-selection feature was removed; clear any
// residual localStorage it wrote (both key generations) so no client-side trace remains.
try {
  localStorage.removeItem('lore.agent.pendingSelectionBySession.v1');
  localStorage.removeItem('lore.agent.regionBySession.v2');
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
  // The queue and status are the active chat's too — the same transition
  // drops them (the INVARIANT in inbox-slice.ts); the load that follows
  // seeds the new chat's own. Same ordering contract as the timeline: the
  // piggyback activates first, seeds second.
  resetInbox(patch => useChatStore.setState(patch));
});

export const chatAttachSurface: AttachSurface = {
  getPending: () => useChatStore.getState().pendingImages,
  add: (dataUrl, meta) => useChatStore.getState().addPendingImage(dataUrl, meta),
  pressing: delta => useChatStore.setState(s => ({ pressingImages: s.pressingImages + delta })),
};
