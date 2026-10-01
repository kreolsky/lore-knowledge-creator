/** Chat input area — textarea with send, stop, mic buttons + model selector + apply-mode dropdown (confirm/auto, derived from PROJECT system-prompts folder) + multi-doc context picker. Chat-store slices: isStreaming, activeSessionId, pendingInputFocus, pendingImages, addPendingImage, removePendingImage, clearPendingImages, draft, setDraft, models, defaultModel, sessions, updateSession, createSession, sendMessage, stopGeneration, setSessionMode. App-store slices: currentProject, currentDocument, currentReference, documents, accessLevel. Context: useChatContext (reactive read), pruneContext. Picker writes directly via addItemToContext / removeItemFromContext. */

import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { BookPlus, EyeOff, ScrollText } from 'lucide-react';
import { Button, Dropdown, FieldCheckbox } from '../ui';
import { ChatComposer } from './ChatComposer';
import { SelectionPill } from './SelectionPill';
import { TokenUsageGauge } from './TokenUsageGauge';
import { CacheHitIndicator, lastTurnCacheHit } from './CacheHitIndicator';
import { useChatStore } from '../../store/chat-store';
import type { ChatUIMode } from '../../types';
import { deriveUIMode } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { readRefOpenMode, refIsScope } from '../../store/ui-store/documents-slice';
import { useTranslation } from '../../i18n';
import { useSimpleVoiceRecording } from '../../hooks/useSimpleVoiceRecording';
import { ContentPickerPopup } from './ContentPickerPopup';

import { useChatContext, pruneContext, computeContextPrune, GHOST_SESSION_ID } from '../../chat/context';
import { resolveContentPickerAnchor } from '../../chat/picker-anchor';
import { useGhostChatContext } from '../../chat/use-ghost-context';
import { canSend as computeCanSend } from '../../chat/send-gate';
import { ghostPinTransfer } from '../../chat/ghost-pin-transfer';
import { on, off } from '../../events';
import { clarifyBlock } from './clarify-format';
import { totalAttachmentBytes, attachmentBudgetExceeded, formatAttachmentMb } from '../../utils/attachment-size';
import { selectActivePath } from '../../store/chat-store/tree';
import { effectiveCap } from '../../store/chat-store/misc-slice';
import { useSystemPrompts, useAttachmentBudget } from './chat-input-hooks';

// Stable empty-array reference for the `queued` selector's empty
// branch (see the selector's INVARIANT — a fresh [] loops useSyncExternalStore).
const NO_QUEUE: string[] = [];

export function ChatInput() {
  // The composer text lives in chat-store (ONE shared draft), not local useState:
  // ChatPanel unmounts on right-panel tab switch and local state dies with it
  // (see SYSTEM: chat-draft). String selector → value-stable, so no NO_QUEUE-style
  // stable-reference invariant is needed. Cleared on send and on store reset().
  const text = useChatStore(s => s.draft);
  const setDraft = useChatStore(s => s.setDraft);
  const { t } = useTranslation();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Voice transcription APPENDS to the store draft (read-append-write is safe:
  // the callback is not concurrent with typing).
  const onTranscribed = useCallback((transcribed: string) => {
    const draft = useChatStore.getState().draft;
    useChatStore.getState().setDraft(draft + (draft ? ' ' : '') + transcribed);
  }, []);
  const { recording, transcribing, toggleRecording } = useSimpleVoiceRecording(onTranscribed);

  const isStreaming = useChatStore(s => s.streaming !== null);
  const activeSessionId = useChatStore(s => s.activeSessionId);
  // Last completed turn's prompt-cache share, off dsh's turn-tail node in the
  // published conversation (see SYSTEM: dsh-conversation) — the assembler
  // derives the per-turn aggregate; nothing is summed here.
  const conversation = useChatStore(s => s.conversation);
  const cacheHit = useMemo(() => lastTurnCacheHit(conversation), [conversation]);
  const pendingInputFocus = useChatStore(s => s.pendingInputFocus);
  const images = useChatStore(s => s.pendingImages);
  const addPendingImage = useChatStore(s => s.addPendingImage);
  const removePendingImage = useChatStore(s => s.removePendingImage);
  const clearPendingImages = useChatStore(s => s.clearPendingImages);
  // The active session's follow-up chips. Keyed by session so a
  // switch never surfaces another chat's chips (ChatInput is mounted once, no key).
  // INVARIANT: the empty branch returns a STABLE module-level reference, not a fresh [].
  // Why: Zustand uses useSyncExternalStore, which compares the selector result by
  // identity — a fresh [] each call reads as "the store changed during render" and loops
  // (Maximum update depth exceeded). The constant is identical to "no chips".
  const queued = useChatStore(s => (s.activeSessionId ? s.queued[s.activeSessionId] ?? NO_QUEUE : NO_QUEUE));
  const removeQueued = useChatStore(s => s.removeQueued);

  useEffect(() => {
    if (pendingInputFocus) {
      textareaRef.current?.focus();
      useChatStore.setState({ pendingInputFocus: false });
    }
  }, [pendingInputFocus]);

  // INVARIANT: the intro header is inserted only before the FIRST clarification in
  // the current draft; the ref resets on send and on session switch.
  // Why: otherwise the header would be duplicated before every appended question.
  const hasClarificationsRef = useRef(false);
  useEffect(() => {
    hasClarificationsRef.current = false;
  }, [activeSessionId]);

  useEffect(() => {
    const handler = ({ quote, question }: { quote: string; question: string }) => {
      const block = clarifyBlock(quote, question);
      // Append to the store draft (never overwrite — the user may have typed more).
      const prev = useChatStore.getState().draft;
      let next: string;
      if (!hasClarificationsRef.current) {
        hasClarificationsRef.current = true;
        const intro = t('chatClarifyIntro');
        next = prev ? `${prev}\n\n${intro}\n\n${block}` : `${intro}\n\n${block}`;
      } else {
        next = prev ? `${prev}\n\n${block}` : block;
      }
      useChatStore.getState().setDraft(next);
      textareaRef.current?.focus();
    };
    on('chat-clarify-insert', handler);
    return () => off('chat-clarify-insert', handler);
  }, [t]);
  const models = useChatStore(s => s.models);
  // WHY: vision-capable model ids (backend-derived) drive the EyeOff badge (shown on models NOT in this set).
  const visionModels = useChatStore(s => s.visionModels);
  const contextWindows = useChatStore(s => s.contextWindows);
  // WHY: per-model reasoning capability (backend-projected from the gateway's
  // /v1/capabilities) drives the effort dropdown. {} → no dropdown (feature
  // absence, not degradation).
  const reasoning = useChatStore(s => s.reasoning);
  const defaultModel = useChatStore(s => s.defaultModel);
  const maxAttachmentMb = useAppStore(s => s.maxAttachmentMb);
  // Audit fix #2: proactively gate the Agent option on agent-service availability.
  const agentAvailable = useChatStore(s => s.agentAvailable);
  const agentUnavailableReason = useChatStore(s => s.agentUnavailableReason);
  const activeSession = useChatStore(s => s.sessions.find(ss => ss.session_id === s.activeSessionId));
  const updateSession = useChatStore(s => s.updateSession);
  const createSession = useChatStore(s => s.createSession);
  const sendMessage = useChatStore(s => s.sendMessage);
  const stopGeneration = useChatStore(s => s.stopGeneration);
  const setSessionMode = useChatStore(s => s.setSessionMode);
  const setSessionUIMode = useChatStore(s => s.setSessionUIMode);
  // Ghost overrides: pre-send config held client-side.
  const ghostAgentAuto = useChatStore(s => s.ghostAgentAuto);
  const ghostSystemPromptId = useChatStore(s => s.ghostSystemPromptId);
  const ghostModel = useChatStore(s => s.ghostModel);
  const ghostReasoningEffort = useChatStore(s => s.ghostReasoningEffort);
  const setGhostSystemPrompt = useChatStore(s => s.setGhostSystemPrompt);
  const startGhostChat = useChatStore(s => s.startGhostChat);
  const projectId = useAppStore(s => s.currentProject?.project_id);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id);
  const rawReference = useAppStore(s => s.currentReference);
  const accessLevel = useAppStore(s => s.accessLevel);
  // Panel quick preview: the chat is the DOCUMENT's. The open reference reads
  // through the ONE projection — session materialization, the agent target and
  // the picker's default tab all follow the doc scope; the ref is added by hand
  // through the picker like any material.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocId ?? '']));
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;
  // Ghost: when no session is active, context is DERIVED from the open entity
  // (useGhostChatContext) — it is never stored. The content picker writes manual
  // deltas (add/removeGhostDelta) that fold into the derived value; at
  // materialization (createSession) the derived value is snapshotted onto the real
  // session id. A materialized chat keeps its stored snapshot (useChatContext).
  const contextSessionId = activeSessionId ?? GHOST_SESSION_ID;
  const storedContext = useChatContext(activeSessionId);
  const ghostContext = useGhostChatContext();
  const activeContext = activeSessionId ? storedContext : ghostContext;

  const activeSystemPromptId = activeSession?.system_prompt_id ?? null;
  const effectiveSystemPromptId = activeSession ? activeSystemPromptId : ghostSystemPromptId;
  const documents = useAppStore(s => s.documents);
  const references = useAppStore(s => s.references);
  // ARCH: a persona is
  // any DIRECT child document of the Personas folder (the reserved doc whose
  // system_role === 'system_prompt'), identified by parent_id — NOT by a role
  // tag. Plain docs (no is_system/role) added there are selectable personas. The
  // legacy project prompts-folder picker (projects.system_prompts_doc_id) is
  // removed. Personas ≡ system prompts: chat_sessions.system_prompt_id is the
  // selected persona doc id. (Derived state lives in useSystemPrompts.)
  const { systemPrompts, activeSystemPromptTitle } = useSystemPrompts(documents, effectiveSystemPromptId);

  const sessionRefId = activeSession?.reference_id ?? currentReference?.reference_id ?? null;
  // INVARIANT: content picker is anchored to the OPEN document (currentDocId) —
  // session-based resolution (ref owning doc → session doc → sessionRefId owning
  // doc) is fallback-only when no document is open (project-level view). Why:
  // the picker ranks by proximity to what the user is currently looking at, not
  // to the session's binding; a ref id is not a valid tree anchor (the picker
  // browses a document's tree).
  const anchorDocId = useMemo(
    () => resolveContentPickerAnchor(currentDocId ?? null, activeSession, sessionRefId ?? null, references),
    [currentDocId, activeSession, sessionRefId, references],
  );
  // WHY: The content picker has no local draft state. Every checkbox click
  // inside ContentPickerPopup writes directly to the session context via
  // addItemToContext / removeItemFromContext (chat/context.ts), with a 200ms
  // debounced PATCH. There is no "cancel draft on close" affordance —
  // intentional, so the same pipeline serves manual edits, auto-population on
  // session creation, and "Chat with Reference" force-add.
  const [contentPickerOpen, setContentPickerOpen] = useState(false);
  const contentPickerRef = useRef<HTMLDivElement>(null);

  // WHY: Prune context IDs that point to entities no longer in the project.
  // Documents are checked against the project-wide `documents` array. References
  // are pruned only when soft-deleted (deletedRefIds) — NEVER merely absent from
  // the open document's `references`, because a selectable cross-doc reference
  // is permanently outside that scope. Backend also skips invalid IDs at
  // completion time; this keeps the count honest in the UI.
  // The prune DECISION lives in computeContextPrune (pure, unit-tested) so this
  // effect stays a thin wire of store reads → pruneContext call.
  useEffect(() => {
    if (!activeSessionId) return;
    if (contentPickerOpen) return;
    if (!activeSession || activeSession.project_id !== projectId) return;
    // INVARIANT (context-race): a context id is pruned only when the SERVER says
    // which ids are references (context_reference_ids); the open document's
    // reference scope is never evidence of a reference's existence. Why: cross-
    // doc refs are selectable (Part B) and are permanently outside that scope,
    // so scope-absence would PATCH away live selections. computeContextPrune
    // encodes both the documents-loaded guard and the server-split gate; when it
    // returns null the effect must NOT call pruneContext / setContextForSession.
    const decision = computeContextPrune(
      activeContext,
      activeSession.context_reference_ids,
      documents,
      useAppStore.getState().deletedRefIds,
    );
    if (decision) {
      pruneContext(activeSessionId, decision.aliveDocIds, decision.deletedRefIds);
    }
  }, [activeSessionId, activeSession, projectId, documents, activeContext, contentPickerOpen]);

  const handleOpenContentPicker = useCallback(() => {
    setContentPickerOpen(prev => !prev);
  }, []);

  const handleContentPickerClose = useCallback((reason?: 'esc') => {
    setContentPickerOpen(false);
    if (reason === 'esc') textareaRef.current?.focus();
  }, []);

  const canAgent = accessLevel === 'full' && agentAvailable;
  // WHY (agent_auto persistence): the dropdown state is DERIVED from
  // the persisted agent_auto column via deriveUIMode, so it survives reload.
  // With one AI line, deriveUIMode reads only agent_auto (no mode).
  // Ghost (no session): derive from the client-side
  // ghostAgentAuto override so the dropdown reflects the pre-send choice.
  const uiMode: ChatUIMode = useMemo(() => {
    if (activeSession) return deriveUIMode(activeSession);
    return deriveUIMode({ agent_auto: ghostAgentAuto });
  }, [activeSession, ghostAgentAuto]);

  // WHY: ghost model is client-side.
  // The model dropdown is editable in a ghost — handleModelChange writes
  // ghostModel — and visibly reflects the inherited model so what the user sees
  // is what createSession POSTs (no silent backend drift).
  const displayModel = activeSession?.model || ghostModel || defaultModel || models[0] || '';

  // WHY: the reasoning-effort dropdown's model is the SAME model the composer
  // displays (displayModel), so the advertised levels always match what a send
  // would use. Renders only for a model advertising levels; absent entry,
  // supported=false, or an empty list all hide the control (no
  // Off, no switch — Default + advertised levels verbatim).
  const reasoningLevels = reasoning[displayModel]?.supported
    ? reasoning[displayModel].effort_levels
    : [];
  // The effective effort mirrors displayModel: the SESSION row's pinned value
  // in a materialized chat, the ghost override pre-send. null = Default (no
  // reasoning_effort on the wire, the provider's default applies — never sent
  // explicitly).
  const displayReasoningEffort = activeSession
    ? activeSession.reasoning_effort ?? null
    : ghostReasoningEffort;

  // WHY: attach-time validation against the shared attachment budget
  // (raw bytes; source = GET /chat/models → maxAttachmentMb). Send is disabled and
  // an in-composer error is shown while over the limit, so the user gets explicit
  // feedback instead of a silent 413 at send time.
  // INVARIANT: the budget is the WHOLE projected body's image sum (history that
  // rides along in apiMessages + new pending), NOT just the new turn's images.
  // Why: the middleware caps the full request body; every send builder ships all
  // non-deleted history images as LLM context, so they count against the cap too.
  // (Derived state lives in useAttachmentBudget.)
  const historyMessages = useChatStore(selectActivePath);
  const {
    projectedAttachmentBytes,
    contextOverLimit,
    attachmentsOverLimit,
    historyBytes,
  } = useAttachmentBudget(maxAttachmentMb, historyMessages, images);

  // INVARIANT: a selected persona ALONE permits an  Why: a persona is a complete instruction with doc metadata attached, so a persona-scoped turn may send an empty body; without a persona an empty send is blocked.
  // empty message — a persona is a complete instruction and the working
  // document's metadata is always attached to a doc-scoped turn. The legacy
  // `&& hasContext` clause is dropped. Why: the common flow is "pick a persona
  // + an open document"; requiring extra attached content would block it. Pure
  // logic lives in chat/send-gate.ts (unit-tested).
  const canSend = computeCanSend({
    text,
    imagesCount: images.length,
    systemPromptId: effectiveSystemPromptId,
    overLimit: attachmentsOverLimit,
    contextOverLimit,
  });

  const handleNewChat = useCallback(() => {
    // WHY: the conversation history alone overflows the attachment budget —
    // start a fresh ghost so the user can send again. No POST; materialization
    // is on first send (lazy everywhere).
    startGhostChat();
  }, [startGhostChat]);

  const handleSend = useCallback(async () => {
    if (!canSend) return;
    // WHY: a chat_sessions ROW is materialized ONLY here, as the first step
    // of the first send (createSession is the single POST entry) — so every
    // PERSISTED session, hence every row in the chat list, carries >= 1 message.  Why: materializing the row only on first send guarantees every persisted session has >=1 message, so the chat list never shows empty (ghost) sessions.
    // The zero/ghost chat is client-only (activeSessionId === null) and never
    // listed. Why: consumers relying on "listed => has messages" (e.g. the chat
    // plaque last-message hover preview) must not have to special-case an empty
    // chat; a bare POST /sessions without a send would break this and re-surface
    // the recurring "empty chat in the list" question.
    // Ghost materialization: the single point where a real session row is
    // created. Guarded on projectId + an open doc OR ref so the scope is known.
    // Forwards the open reference id when a ref is the active scope so the
    // materialized session is ref-scoped (backend stores document_id = ref id).
    if (!activeSessionId && projectId && (currentDocId || currentReference?.reference_id)) {
      // createSession now resolves null on failure (own toast) instead of
      // throwing — guard it or setDraft('') below would wipe the user's input.
      // Pass the ghost overrides (agent_auto/system-prompt + agent target =
      // the open entity) so the materialized row reflects the pre-send config.
      const ghost = useChatStore.getState();
      const targetDocId = currentReference?.reference_id ?? currentDocId;
      // Ghost-pin transfer: if the ghost carries an
      // in-memory pinned region, materialize it as a has_region agent session and forward
      // the region so createSession writes it to pending-selection BEFORE activation
      // (the ordering invariant). Mirrors the ghost-context snapshot on
      // materialization. Every ghost is an agent chat, so the pin transfers whenever
      // the ghost carries one (no mode gate).
      const pin = ghostPinTransfer(ghost.ghostRegion);
      const created = await createSession({
        projectId,
        documentId: currentDocId,
        model: ghost.ghostModel || undefined,
        // The ghost's explicit reasoning effort rides the same POST; omitted
        // (null) leaves the column absent = Default.
        reasoningEffort: ghost.ghostReasoningEffort ?? undefined,
        systemPromptId: ghost.ghostSystemPromptId,
        referenceId: currentReference?.reference_id,
        agentAuto: ghost.ghostAgentAuto,
        // The agent target is ALWAYS the open entity
        // (currentReference ?? currentDocument), never the source chat's
        // target_doc_id.
        targetDocId,
        ...(pin ?? {}),
      });
      if (!created) return;
      // Transfer complete: the pin now lives on the materialized session (localStorage);
      // drop the in-memory ghost pin so the pill/highlight read the materialized source.
      if (pin) useChatStore.getState().clearGhostRegion();
    }
    const imgs = images.length > 0 ? images : undefined;
    // When a turn is streaming, sendMessage enqueues the TEXT
    // (images stay in the composer — attachment-budget merge is a separate problem, see
    // plan Scope). Capture streaming BEFORE the call so the images are kept (not silently
    // dropped) on the enqueue path; only a real send consumes them.
    const wasStreaming = isStreaming;
    sendMessage(text.trim(), imgs);
    // Clear the store draft (also on the streaming-enqueue path — current behavior).
    setDraft('');
    hasClarificationsRef.current = false;
    if (!wasStreaming) clearPendingImages();
  }, [canSend, activeSessionId, projectId, currentDocId, currentReference, text, images, createSession, sendMessage, clearPendingImages, setDraft, isStreaming]);
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSend();
    } else if (e.key === 'Escape' && isStreaming) {
      // Esc = stop, always — regardless of composer contents.
      // Without this, the button rule (Send shown whenever text is present) would cost a
      // one-click stop mid-turn. The send hotkey is Cmd/Ctrl+Enter, so Esc is free.
      e.preventDefault();
      stopGeneration();
    }
  }, [handleSend, isStreaming, stopGeneration]);

  const readImageFile = useCallback((file: File) => {
    // WHY: enforce the shared attachment budget (sum of all images) at
    // attach time. file.size is the new file's raw bytes; add the already-queued
    // images' estimated bytes. Reject (with explicit feedback) before adding.
    const pending = totalAttachmentBytes(useChatStore.getState().pendingImages);
    if (attachmentBudgetExceeded(pending, file.size, maxAttachmentMb)) {
      useAppStore.getState().showToast(
        t('attachmentsExceedLimit', { used: formatAttachmentMb(pending + file.size), limit: maxAttachmentMb }),
        'error',
      );
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === 'string') {
        addPendingImage(reader.result as string);
      }
    };
    reader.readAsDataURL(file);
  }, [maxAttachmentMb, t, addPendingImage]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) readImageFile(file);
      }
    }
  }, [readImageFile]);


  const handleModelChange = useCallback((model: string) => {
    if (activeSessionId) {
      // ARCH: model change RESETS the effort —
      // {model, reasoning_effort: null} in ONE PATCH. Why: a stale level under
      // a non-reasoning model would pass the router untouched and die upstream;
      // null is always legal (the backend guard rejects only non-null values).
      updateSession(activeSessionId, { model, reasoning_effort: null });
    } else {
      // Ghost: hold the choice client-side so it survives to materialization;
      // the ghost effort clears with the model for the same reason.
      useChatStore.setState({ ghostModel: model, ghostReasoningEffort: null });
    }
  }, [activeSessionId, updateSession]);

  // WHY: reasoning-effort select handler — mirrors handleModelChange. In place
  // (PATCH reasoning_effort) for a materialized session; client-side
  // (ghostReasoningEffort) in a ghost, applied at materialization. The empty
  // string is the Default option → null on the wire.
  const handleReasoningSelect = useCallback((effort: string) => {
    const value = effort === '' ? null : effort;
    if (activeSessionId) {
      updateSession(activeSessionId, { reasoning_effort: value });
    } else {
      useChatStore.setState({ ghostReasoningEffort: value });
    }
    textareaRef.current?.focus();
  }, [activeSessionId, updateSession]);

  // WHY: "auto-apply changes" checkbox — the surviving agent_auto selector.
  // Checked = every mutating tool applies without asking (agent_auto); unchecked
  // = the agent confirms each mutating tool first (the safe default). Gated on
  // full project access in the store AND here (the checkbox is disabled for
  // non-full users).
  const autoApply = uiMode === 'agent_auto';
  const handleAutoApplyToggle = useCallback((checked: boolean) => {
    setSessionUIMode(checked ? 'agent_auto' : 'agent_confirm');
    textareaRef.current?.focus();
  }, [setSessionUIMode]);

  // WHY: system-prompt select handler. A named system prompt is framing applied
  // in place (setSessionMode PATCHes system_prompt_id). "Default" (empty) clears it.
  // There is no mode boundary to cross — every AI chat is an agent chat.
  // In a ghost (no session) the choice is held client-side
  // (ghostSystemPromptId) and applied at materialization.
  const handlePromptSelect = useCallback((promptId: string) => {
    if (!activeSessionId) {
      setGhostSystemPrompt(promptId === '' ? null : promptId);
    } else {
      setSessionMode(promptId === '' ? null : promptId);
    }
    textareaRef.current?.focus();
  }, [activeSessionId, setSessionMode, setGhostSystemPrompt]);

  const handleModelSelect = useCallback((model: string) => {
    handleModelChange(model);
    textareaRef.current?.focus();
  }, [handleModelChange]);

  // AI-only slots forwarded to the shared ChatComposer. The composer owns the
  // resize handle, textarea fill, attachment chips, mic, and send/stop buttons.
  const topControls = (
    <>
      {(currentDocId || activeSessionId) && (
        <div className="flex justify-between items-center gap-3 mb-2 flex-wrap">
          <div className="flex gap-3">
            <div ref={contentPickerRef}>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleOpenContentPicker}
                disabled={isStreaming}
              >
                <BookPlus size={14} className="mr-1" />
                {activeContext.documentIds.length > 0 || activeContext.referenceIds.length > 0
                  ? t('addContentMixedCount', { docs: activeContext.documentIds.length, refs: activeContext.referenceIds.length })
                  : t('addContent')}
              </Button>
            </div>
            {(currentDocId || activeSessionId) && (
              <Dropdown
                value={effectiveSystemPromptId ?? ''}
                options={[
                  { value: '', label: t('chatSystemPromptNone') },
                  ...systemPrompts.map(p => ({ value: p.document_id, label: p.title })),
                ]}
                onSelect={handlePromptSelect}
                icon={<ScrollText size={14} />}
                variant="ghost"
                triggerLabel={activeSystemPromptTitle ?? t('chatSystemPromptNone')}
                disabled={isStreaming}
                placement="bottom"
              />
            )}
          </div>
          {/* Token-usage gauge: same row as the context/persona controls, RIGHT
              side. AI-chat only (activeSessionId); fresh session shows 0 / cap.
              The cap prefers the session's context_window — the LAST
              context_usage frame's cap (the model's real window per the
              harness projection) — with effectiveCap(...) as the
              pre-first-frame fallback. */}
          {activeSessionId && (
            <div className="flex items-center gap-3">
              <TokenUsageGauge
                used={activeSession?.context_tokens_used ?? 0}
                cap={activeSession?.context_window ?? effectiveCap(
                  contextWindows,
                  activeSession?.model ?? displayModel,
                )}
              />
              {/* Prompt-cache hit share of the last turn, beside the gauge. Absent
                  until a turn completes with cache accounting (see CacheHitIndicator). */}
              {cacheHit && <CacheHitIndicator figure={cacheHit} />}
            </div>
          )}
        </div>
      )}
      {contentPickerOpen && contentPickerRef.current && (activeSessionId || currentDocId) && (() => {
        const isRefActive = !!currentReference;
        const initialTab: 'references' | 'documents' = isRefActive ? 'references' : 'documents';
        if (import.meta.env.DEV) {
          // Spec §2: ContentPicker default tab must match the type of the open object.
          const expected = isRefActive ? 'references' : 'documents';
          if (initialTab !== expected) {
            console.error('[chat §2] ContentPicker default tab mismatch', { initialTab, expected, currentReference });
          }
        }
        return (
          <ContentPickerPopup
            sessionId={contextSessionId}
            selectedDocIds={activeContext.documentIds}
            selectedRefIds={activeContext.referenceIds}
            onClose={handleContentPickerClose}
            anchorRect={contentPickerRef.current.getBoundingClientRect()}
            triggerRef={contentPickerRef}
            initialTab={initialTab}
            anchorDocId={anchorDocId}
            sessionRefId={sessionRefId}
            above
          />
        );
      })()}
    </>
  );

  const warnings = (
    <>
      <SelectionPill />
      {attachmentsOverLimit && (
        <div className="mb-2 text-ui-sm text-danger px-2 py-1 border border-danger/40 bg-danger/5">
          {t('attachmentsExceedLimit', {
            used: (projectedAttachmentBytes / (1024 * 1024)).toFixed(1),
            limit: maxAttachmentMb,
          })}
        </div>
      )}

      {contextOverLimit && (
        <div className="mb-2 text-ui-sm text-danger px-2 py-1 border border-danger/40 bg-danger/5 flex items-center justify-between gap-2">
          <span>
            {t('chatContextImagesFull', {
              used: (historyBytes / (1024 * 1024)).toFixed(1),
              limit: maxAttachmentMb,
            })}
          </span>
          <Button variant="ghost" size="sm" onClick={handleNewChat}>
            {t('chatContextFullNewChat')}
          </Button>
        </div>
      )}

      {/* Audit fix #2 (A5): when the active AI chat is open but the agent service
          became unavailable, surface a notice. Every AI chat is an agent chat, so
          this shows whenever the agent service is down (no readonly/ask fallback
          exists). */}
      {activeSessionId && !agentAvailable && (
        <div className="mb-2 text-ui-sm text-amber px-2 py-1 border border-amber/40 bg-amber/5">
          {t('chatAgentUnavailable')}
        </div>
      )}
    </>
  );

  const leftControls = (
    <>
      {models.length > 0 && (
        <Dropdown
          value={displayModel}
          options={models.map(m => ({
            value: m,
            label: m,
            // WHY: EyeOff badge marks models WITHOUT vision — the ones the
            // backend's image-stripping gate strips images for. Vision is the
            // common case, so it carries no mark. Same badge reaches the
            // collapsed trigger via options[selectedIndex]?.badge.
            badge: visionModels.includes(m)
              ? undefined
              : <EyeOff size={11} className="opacity-50 shrink-0" aria-label={t('chatModelNoVision')} />,
          }))}
          onSelect={handleModelSelect}
          disabled={isStreaming}
          placement="top"
        />
      )}
      {reasoningLevels.length > 0 && (
        <Dropdown
          value={displayReasoningEffort ?? ''}
          options={[
            // Default (null) first, then the model's advertised levels verbatim
            // (the router owns the vocabulary; no Off, no switch).
            { value: '', label: t('chatReasoningDefault') },
            ...reasoningLevels.map(lv => ({ value: lv, label: lv })),
            // WHY: a row/ghost effort OUTSIDE the model's advertised list (raw-API
            // model switch without the reset PATCH, cross-client staleness) must
            // render as its OWN option — the Dropdown clamps an unknown value to
            // the first option, which would display Default while the wire sends
            // the foreign level (silent degradation of exactly the kind the repo
            // forbids). Shown verbatim after the advertised ones; picking Default
            // clears it, re-picking it re-PATCHes it and the backend 400s honestly.
            ...(displayReasoningEffort && !reasoningLevels.includes(displayReasoningEffort)
              ? [{ value: displayReasoningEffort, label: displayReasoningEffort }]
              : []),
          ]}
          onSelect={handleReasoningSelect}
          disabled={isStreaming}
          title={t('chatReasoningTitle')}
          placement="top"
        />
      )}
      <FieldCheckbox
        checked={autoApply}
        onChange={handleAutoApplyToggle}
        label={t('chatAutoApply')}
        disabled={!canAgent || isStreaming}
        title={!canAgent && accessLevel === 'full' && !agentAvailable
          ? (agentUnavailableReason ?? t('chatAgentDisabledReason'))
          : t('chatAutoApplyHint')}
      />
    </>
  );

  return (
    <ChatComposer
      variant="ai"
      highlight={!activeSessionId}
      value={text}
      onChange={e => setDraft(e.target.value)}
      onSend={handleSend}
      onStop={stopGeneration}
      onKeyDown={handleKeyDown}
      onPaste={handlePaste}
      isStreaming={isStreaming}
      canSend={canSend}
      placeholder={contextOverLimit ? t('chatContextFullPlaceholder') : (activeSessionId ? t('chatPlaceholder') : t('chatPlaceholderNew'))}
      disabled={contextOverLimit}
      recording={recording}
      transcribing={transcribing}
      onToggleRecording={toggleRecording}
      images={images}
      onRemoveImage={removePendingImage}
      queued={queued}
      onRemoveQueued={i => { if (activeSessionId) removeQueued(activeSessionId, i); }}
      topControls={topControls}
      leftControls={leftControls}
      warnings={warnings}
      textareaRef={textareaRef}
    />
  );
}

