/** ChatState interface and Set/Get aliases for slice creators. */
import type { ChatUIMode, ChatSession, ChatMessage, Reference, PinnedRegion, ReasoningCapability } from '../../types';
import type { ConversationVM, TurnRange } from './conversation-feed';

/**
 * Why a turn ended — read at the turn's terminal, off the harness
 * REGISTRATION's end facts. 'done' is the DEFAULT: nothing is stamped
 * through a clean turn. 'error'/'halted' are stamped by their frame handlers;
 * 'aborted' is stamped by stopGeneration before it aborts; 'lost' is a turn
 * whose terminal never reached this tab (the WS-gap close). The queue
 * flush decision hangs on this value: only 'done' auto-fires.
 */
export type TurnEndReason = 'done' | 'aborted' | 'error' | 'halted' | 'lost';

/**
 * The single streaming state machine object.
 *
 * It stands in for the former flat fields (isStreaming/streamingContent/
 * streamingMessageId/abortController). `ChatState.streaming`
 * is this object while a turn is in flight, or `null` when idle.
 * `streaming === null` ⟺ the former `isStreaming === false`.
 */
export interface StreamingState {
  // The chat whose turn this slot shows. The slot belongs to the ACTIVE
  // chat only: a turn running in a chat that is not shown holds NO slot (its
  // facts live on the harness registration), so leaving a streaming chat
  // drops the slot and never the turn.
  sessionId: string;
  /** The assistant message id deltas accumulate onto (former streamingMessageId). */
  messageId: string | null;
  /** Accumulated assistant text (former streamingContent). */
  content: string;
  /** The in-flight AbortController (former abortController). */
  controller: AbortController | null;
}

/**
 * Single options object for createSession (every entry point: ChatInput
 * lazy-create, startAgentChat, setSessionMode agent branch, "+"). Replaces the
 * prior 7-positional-arg signature.
 *
 * INVARIANT (explicit-null-vs-omitted): systemPromptId / parentSessionId use
 * `string | null | undefined` — `undefined` (omitted) means "inherit" (backend
 * resolver walks Reference → parent Document → defaults), while an explicit
 * `null` means "use default" (no prompt / no parent). The POST body is built
 * off `systemPromptId !== undefined` so the backend keeps distinguishing the two.
 */
export interface CreateSessionParams {
  projectId: string;
  documentId?: string;
  model?: string;
  systemPromptId?: string | null;
  referenceId?: string;
  parentSessionId?: string | null;
  focus?: boolean;
  agentAuto?: boolean;
  targetDocId?: string;
  hasRegion?: boolean;
  // The ghost's explicit reasoning effort.
  // Only a REAL value is ever passed; omitted = Default (the POST leaves the
  // column absent). Unlike systemPromptId there is no null-vs-undefined
  // distinction to carry — both mean Default here.
  reasoningEffort?: string;
  // The frontend-owned pinned region to persist for this session. Written to
  // pending-selection (localStorage) SYNCHRONOUSLY right after the session becomes
  // active — see the ordering INVARIANT in createSession.
  // Passed by ChatInput.handleSend when materializing a ghost that has a pin.
  region?: PinnedRegion;
}

export interface ChatState {
  // Sessions
  sessions: ChatSession[];
  // INVARIANT: activeSessionId must reference an entry in sessions or be null  Why: a dangling id makes every sessions.find(...) return undefined; the chat UI resolves the active session off this pointer, so it must always resolve or be null (ghost state).
  activeSessionId: string | null;
  // Current document whose sessions are loaded
  documentId: string | null;

  // Messages
  // WHY: should only contain messages belonging to activeSessionId  Why: messages[] is the active session's display buffer, swapped wholesale on switch; mixing another session's messages here would leak them across sessions.
  messages: ChatMessage[];
  messagesLoading: boolean;
  // WHY: true only after a loadMessages FAILURE; cleared on a successful load,
  // session switch, and load entry. Why: a load failure otherwise leaves messages=[]
  // + messagesLoading=false → MessageList falls through to the empty state, making an
  // error indistinguishable from "no messages" once the toast dismisses (empty state
  // must look distinct from error state).
  messagesError: boolean;
  // INVARIANT: a single "scope is resolving" gate spanning loadSessions entry →  Why: one spanning gate keeps MessageList on a single spinner so it never flashes the empty start-state mid-load on a document change (empty vs loading must stay distinct).
  // resolver → loadMessages, so MessageList shows ONE spinner and never flashes
  // its empty ("start conversation") state mid-load on a document change. Set
  // true synchronously in loadSessions only on a real document change; cleared at
  // every terminal point that does not reach loadMessages, and by loadMessages
  // when its fetch settles. Why: empty state (no chats) must be distinguishable
  // from loading state — deriving empty from messages.length===0 alone flashes.
  chatScopeLoading: boolean;

  // WHY: selectedSiblings maps parentId -> chosen messageId for fork navigation  Why: for branched chats (siblings sharing a parentId), records which sibling the user navigated to so fork-nav shows the chosen branch, not an arbitrary one.
  selectedSiblings: Record<string, string>;

  // Streaming: the tightly-coupled streaming state machine lives in ONE object so
  // it is manipulated as a unit — split across flat fields, a reset could clear
  // `streamingMessageId` but leave `streamingContent`, etc.
  // `streaming === null` means "not streaming".
  // WHY: streaming.messageId + content + controller
  // are set together and cleared together; never partially.  Why: a partial clear leaves zombie streaming state (controller dropped while content lingers, or a stuck streaming flag), so they are set/cleared as one atomic unit.
  // INVARIANT(corruption): streaming === null || streaming.sessionId === activeSessionId.
  // Why: the slot renders the ACTIVE chat's turn; a slot owned by another chat
  // would make the composer/Stop of the shown chat drive a turn it cannot see,
  // and a turn left behind would clobber the next chat's slot at its terminal.
  // Every write of activeSessionId clears the slot or seats one for the new id.
  streaming: StreamingState | null;

  // The dsh assembler's published nodes (SYSTEM: dsh-conversation) — the ONE
  // timeline the AI chat renders over. Plain-data projection of the engine's
  // node list; the engine itself is module-level (conversation-feed.ts), never
  // Zustand state. Published per the assembler's own cadence (animation-frame
  // batches streaming chunks; immediate on boundaries).
  conversation: ConversationVM[];
  // Assistant message id → the [min, max] seq window of that turn's fed
  // frames. Ownership is inclusive on both ends (turn windows are disjoint);
  // rebuilt wholesale by replaceWindowFromRows on every reload.
  turnRanges: Record<string, TurnRange>;
  // The live turn's boundary: the engine tail when the turn was seated (ids).
  // Nodes anchored above it render in the streaming message; null between
  // turns.
  turnStartSeq: number | null;

  // Models
  models: string[];
  // ARCH: vision-capable model ids (subset of `models`), sourced from GET
  // /chat/models `vision_models` (backend projects the gateway's per-model
  // metadata; the send-time image gate asks the driver — the same roster).
  // Drives the Eye badge in the model picker. Capability flag, not session
  // state → NOT cleared by reset() (mirrors models/agentAvailable).
  visionModels: string[];
  // Per-model real context window (sourced from GET /chat/models `context_windows`,
  // the gateway's `context_length`). The PRIMARY cap source for the gauge
  // (effectiveCap). Capability map, not session state → NOT cleared by reset()
  // (mirrors models/agentAvailable). Only models the gateway reported a window for
  // are present; others fall back.
  contextWindows: Record<string, number>;
  // ARCH: per-model reasoning capability map
  // (GET /chat/models `reasoning`, the backend's projection of the gateway's
  // /v1/capabilities). The effort dropdown's single source — {} when the
  // gateway has no such endpoint (feature absence: no dropdown, no banner).
  // Capability map, not session state → NOT cleared by reset() (mirrors
  // models/visionModels).
  reasoning: Record<string, ReasoningCapability>;
  modelsLoaded: boolean;
  defaultModel: string;
  // NOTE: maxAttachmentMb lives in app-store (single-source).
  // loadModels writes it via useAppStore.setMaxAttachmentMb.
  // SYSTEM: chat-agent-availability. Sourced from GET /chat/models
  // so the composer proactively disables the Agent option when the agent service is
  // unavailable (instead of erroring at send time). Defaults to agentAvailable=false
  // (pessimistic) until loadModels resolves — the Agent option stays disabled until
  // a positive signal arrives, matching the "no silent degradation" rule.
  agentAvailable: boolean;
  agentUnavailableReason: string | null;

  // UI signals
  pendingInputFocus: boolean;
  // The composer's model picker, opened by a turn refused with 403
  // `model_forbidden` so the user picks an available model (ChatInput
  // controls the Dropdown with it).
  modelPickerOpen: boolean;
  pendingImages: string[];
  // SYSTEM: chat-draft — the composer text held in the store (ONE shared string,
  // session-agnostic) so it survives ChatPanel unmount on right-panel tab switches.
  // Mirrors the prior in-mount useState semantics (text already crossed session
  // switches). In-memory only: cleared on send and on reset() — F5 drops it (the
  // user's call, consistent with pendingImages). Contrast note-chat's
  // PER-SESSION drafts (a note draft is thread-scoped).
  draft: string;

  // SYSTEM: chat-message-queue — follow-up chips typed while a turn streams, keyed by
  // session. Why keyed: the composer draft is ONE shared string that crosses a session
  // switch; a flat queue would surface chat A's chips inside chat B. Not persisted (F5
  // drops it — the user's call). The backend never learns the queue exists: one
  // ordinary sendMessage fires on flush.
  queued: Record<string, string[]>;
  // Chat-list title filter typed in the ghost header's search mode. null = search
  // mode OFF (the hint shows); '' … = search mode ON with that query. NEVER
  // persisted and NEVER survives leaving the ghost state — ChatHeader clears it on
  // its own unmount and when a chat opens. Why: the user asked for a stateless
  // search (a tab switch / opening a chat drops it).
  listFilter: string | null;

  // SYSTEM: ghost-chat overrides. A ghost
  // (activeSessionId === null) is fully configurable pre-send: the agent_auto
  // toggle + system-prompt + model are held client-side and applied at
  // materialization (createSession). agent target resolves to the open entity.
  // WHY: a fresh ghost VISIBLY  Why: pre-fills model + agent_auto + system_prompt_id from the last active AI chat so a new ghost inherits the user's settings instead of silently resetting to defaults (agent_auto is client-side only — the backend walk doesn't cover it).
  // pre-fills model + agent_auto + system_prompt_id from the LAST ACTIVE
  // AI chat in the current scope. The source is `get().sessions` (current scope,
  // ORDER BY updated_at DESC server-side): the active session if present in
  // scope, else the latest-by-updated_at session; bare defaults when the scope is
  // empty. agent_auto is CLIENT-SIDE ONLY — the backend never inherits it
  // (its latest-of-scope walk covers only model + system_prompt_id). So
  // initGhostFromScope is the single place that bridges the gap; without it a
  // "+" / discard / openChatWithReference ghost would silently drop agent_auto.
  // resetGhostOverrides clears overrides to bare defaults (used by reset()).
  // There is no ghost mode axis; ghostAgentAuto is the only pre-send apply-mode
  // override.
  ghostAgentAuto: boolean;
  ghostSystemPromptId: string | null;
  ghostModel: string;
  // ARCH: the pre-send reasoning-effort
  // override (mirrors ghostModel). null = Default. Cleared TOGETHER with
  // ghostModel on a ghost model change — a stale level under a non-reasoning
  // model would pass the router untouched and die upstream. Inherited
  // client-side from the scope donor at initGhostFromScope (the backend
  // create-walk inherits model + prompt but NOT reasoning_effort, so this is
  // the bridge — same rationale as agent_auto).
  ghostReasoningEffort: string | null;

  // SYSTEM: selection-region-agent (ghost pin). The
  // pinned region for the ZERO/GHOST chat (activeSessionId === null) — held IN-MEMORY,
  // NOT localStorage.
  // INVARIANT(persisted): the ghost region is in-memory only. Why: it must vanish on page refresh
  // (localStorage would survive) and there is no session id to key a map by until the
  // chat materializes; on first send it is transferred to pending-selection (localStorage)
  // keyed by the new session id, mirroring the ghost-context snapshot model.
  ghostRegion: PinnedRegion | null;

  // ARCH (agent_auto persistence): the 2-option apply-mode selector
  // (agent_confirm / agent_auto) is DERIVED from the persisted session row's
  // agent_auto via deriveUIMode() — not held in an in-memory map. The per-session
  // choice survives reload because it is a backend column (mirrors
  // model/system_prompt_id).
  setSessionUIMode: (mode: ChatUIMode) => void;

  // Actions — images (pending attachments before send)
  addPendingImage: (dataUrl: string) => void;
  removePendingImage: (index: number) => void;
  clearPendingImages: () => void;

  // Actions — draft (see the `draft` field by pendingImages)
  setDraft: (v: string) => void;
  setListFilter: (v: string | null) => void;

  // Actions — sessions
  loadSessions: (projectId: string, documentId?: string) => Promise<void>;
  // ARCH: open a fresh client-only ghost (activeSessionId = null) for the open
  // entity. Single entry point for "Add chat", post-delete ghost, discard/reset.
  // Sync-attaches the open entity, resets ghost overrides to defaults. No POST.
  startGhostChat: () => void;
  // Ghost overrides (pre-send config held client-side). Applied at materialization.
  setGhostSystemPrompt: (id: string | null) => void;
  resetGhostOverrides: () => void;
  // ARCH: set the ghost overrides
  // (model/mode/agent_auto/system_prompt_id) from the last active AI chat in the
  // current scope. Called at EVERY ghost entry point (startGhostChat, loadSessions
  // none-branch, deleteSession last-session, openChatWithReference) so a fresh
  // ghost visibly carries the inherited values. Reads activeSessionId BEFORE it is
  // cleared so a "+" from an active chat inherits from the chat being left.
  initGhostFromScope: () => void;
  // ARCH: Composite action — serializes the entire "Chat with Reference" flow
  // into a single awaitable sequence. No second useEffect races with it
  // because the action owns the full order: set ref → load sessions →
  // add ref to context → open panel.
  openChatWithReference: (ref: Reference) => Promise<void>;
  createSession: (params: CreateSessionParams) => Promise<ChatSession | null>;
  // preloadedMessages: when the sessions list piggybacked this session's
  // messages, commit them directly and skip the loadMessages round-trip.
  setActiveSession: (sessionId: string | null, preloadedMessages?: ChatMessage[]) => void;
  updateSession: (sessionId: string, data: { title?: string; model?: string; system_prompt_id?: string | null; context_ids?: string[]; agent_auto?: boolean; has_region?: boolean; reasoning_effort?: string | null; document_id?: string | null }) => Promise<void>;
  deleteSession: (sessionId: string) => Promise<void>;

  // Actions — messages
  loadMessages: (sessionId: string) => Promise<void>;
  // The browser-WS-gap resync — a
  // project-WS reconnect reloads the ACTIVE session's messages when a harness
  // turn is open in this tab (the adoption path re-seats the window; frames
  // lost in the socket gap recover through the reload's replay).
  resyncOpenHarnessTurn: () => Promise<void>;
  sendMessage: (content: string, images?: string[]) => Promise<void>;
  editMessage: (messageId: string, content: string) => Promise<void>;
  deleteMessage: (messageId: string) => Promise<void>;
  forkAndResend: (messageId: string, content: string, images?: string[]) => Promise<void>;
  regenerate: (messageId: string) => Promise<void>;
  stopGeneration: () => void;

  // Actions — forks
  selectSibling: (parentId: string, messageId: string) => void;
  // Rewind-to-message (agent chats): cut the active path so the message and
  // everything below it stop rendering, and the next send forks a SIBLING from
  // the point before it (a REWIND_KEY sentinel in selectedSiblings — the
  // branch is NOT deleted, the fork switcher offers it once the sibling
  // exists). Refused while streaming. In-memory: a reload or session switch
  // clears it with selectedSiblings and the full branch shows again.
  rewindTo: (messageId: string) => void;
  // Drop the rewind sentinel — the path falls back to its previous resolution
  // and the hidden branch reappears.
  cancelRewind: () => void;
  getSiblings: (parentId: string | null) => ChatMessage[];

  // Actions — message queue. Guard lives in the store, not
  // the UI: sendMessage itself routes here when streaming !== null, so the MicButton
  // transcription path and hotkeys can't bypass it.
  enqueueMessage: (sessionId: string, text: string) => void;
  removeQueued: (sessionId: string, index: number) => void;
  clearQueued: (sessionId: string) => void;
  // Auto-fire the coalesced queue for a session at a clean turn-end. Session-owned:
  // brings the user to the owning session (single-session streaming model) then sends.
  flushQueued: (sessionId: string) => Promise<void>;
  // Restore the joined queue text into the composer (abort/error/halted/lost) instead
  // of auto-firing a follow-up into a turn the user just killed.
  restoreQueued: (sessionId: string) => void;

  // Actions — models
  loadModels: () => Promise<void>;
  setModelPickerOpen: (v: boolean) => void;

  // Actions — agent mode
  // (proposal apply actions were deleted with the proposal cluster —
  // confirmation is the mid-turn approval hold.)

  // Mid-turn approval: publish the user's verdict for a held mutating call
  // (allow once / allow for the session / reject with text).
  decideVerdict: (
    callId: string,
    action: 'allow_once' | 'allow_session' | 'reject',
    toolName: string,
    reason?: string,
  ) => Promise<void>;
  // Drop the decision card for a call id (optimistic post-verdict / stale hold).
  removePendingVerdictByCall: (callId: string) => void;
  // Reload re-render: restore the session's still-held calls from the decision
  // store (GET /chat/verdicts) onto their assistant messages.
  fetchPendingVerdicts: (sessionId: string) => Promise<void>;
  // Set the active session's system prompt in place (PATCH system_prompt_id).
  // There is no ask↔agent boundary (one AI line), so this PATCHes only
  // system_prompt_id. The agent_auto toggle is a separate action
  // (setSessionUIMode); a named prompt (systemPromptId != null) implies no mode
  // switch.
  setSessionMode: (systemPromptId?: string | null) => Promise<void>;
  // see SYSTEM: selection-region-agent — open a pinned-region agent chat from an editor
  // selection. Creates a has_region agent session + stores the Yjs RelativePosition
  // pair (frontend-owned, localStorage) so the pin survives same-device reload.
  startAgentChat: (payload: {
    doc_id: string;
    relFrom: unknown;
    relTo: unknown;
    from_cp: number;
    to_cp: number;
    text: string;
  }) => Promise<void>;
  // Unpin the active region: PATCH has_region=false (re-enables auto-apply, drops
  // the containment constraint) + clear the localStorage anchor + remove highlight.
  unpinRegion: (sessionId: string) => Promise<void>;
  // Auto-unpin when the region's anchor is lost after a wholesale restore/import
  // (distinct toast from a user-initiated unpin). Idempotent while pinned.
  regionLost: (sessionId: string) => Promise<void>;
  // Ghost-pin setters. setGhostRegion attaches an
  // in-memory pin to the zero chat; clearGhostRegion drops it (the ✕ on the ghost pill,
  // a lost anchor, a document change, materialization transfer, or reset()).
  setGhostRegion: (region: PinnedRegion) => void;
  clearGhostRegion: () => void;
  // Actions — utility
  reset: () => void;
}

export type Set = (
  partial: Partial<ChatState> | ((s: ChatState) => Partial<ChatState>),
) => void;
export type Get = () => ChatState;
