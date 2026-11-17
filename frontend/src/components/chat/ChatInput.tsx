/** Chat input area — a top row [Persona ▾ (memory state on its icon)] [Hide] [Analyse] … token gauge, then the composer: textarea with send, stop, mic + model / reasoning selectors + auto-apply. Chat-store slices: streaming, dshRunning, inboxQueue, liveSend, discardQueued, activeSessionId, pendingInputFocus, pendingImages, pendingImageMeta, pressingImages, removePendingImage, clearPendingImages, pendingFiles, removePendingFile, clearPendingFiles, draft, setDraft, models, defaultModel, sessions, updateSession, createSession, sendMessage, stopGeneration, setSessionMode, setChatUnindexed, startGhostChat, ghost overrides. App-store slices: currentProject, currentDocument, currentReference, documents, accessLevel. */

import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { Brain, Eye, EyeOff, ScrollText, Sparkles } from 'lucide-react';
import { Button, Dropdown, FieldCheckbox, IconButton } from '../ui';
import { ChatComposer } from './ChatComposer';
import { TokenUsageGauge } from './TokenUsageGauge';
import { CacheHitIndicator } from './CacheHitIndicator';
import { useChatStore, chatAttachSurface } from '../../store/chat-store';
import type { ChatUIMode } from '../../types';
import { deriveUIMode } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useUIStore } from '../../store/ui-store';
import { readRefOpenMode, refIsScope } from '../../store/ui-store/documents-slice';
import { useTranslation } from '../../i18n';
import { useSimpleVoiceRecording } from '../../hooks/useSimpleVoiceRecording';

import { canSend as computeCanSend } from '../../chat/send-gate';
import { on, off, emit } from '../../events';
import { insertAtCaret } from '../../utils/insert-at-caret';
import { attachImage } from '../../chat/attach-image';
import { effectiveCap, appendDraft } from '../../store/chat-store/misc-slice';
import { useSystemPrompts, useAttachmentBudget } from './chat-input-hooks';
import { useMemoryPersonas } from './useMemoryPersonas';


export function ChatInput() {
  // The composer text lives in chat-store (ONE shared draft), not local useState:
  // ChatPanel unmounts on right-panel tab switch and local state dies with it
  // (see SYSTEM: chat-draft). String selector → value-stable. Cleared on send
  // and on store reset().
  const text = useChatStore(s => s.draft);
  const setDraft = useChatStore(s => s.setDraft);
  const { t } = useTranslation();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Voice transcription is inserted at the composer's caret, not appended — the user
  // dictates into the middle of a draft (read-splice-write is safe: the callback is not
  // concurrent with typing). The caret is restored after the controlled value re-renders.
  const onTranscribed = useCallback((transcribed: string) => {
    const el = textareaRef.current;
    const { text: next, caret } = insertAtCaret(useChatStore.getState().draft, transcribed, el);
    useChatStore.getState().setDraft(next);
    requestAnimationFrame(() => el?.setSelectionRange(caret, caret));
  }, []);
  const { recording, transcribing, toggleRecording, cancelRecording } = useSimpleVoiceRecording(onTranscribed);

  // Busy = this tab's turn slot OR dsh running the chat (a turn of another
  // tab, or the gap between two queued turns): Stop shows and stops either.
  const isStreaming = useChatStore(s => s.streaming !== null || s.dshRunning);
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const pendingInputFocus = useChatStore(s => s.pendingInputFocus);
  const images = useChatStore(s => s.pendingImages);
  const pressingImages = useChatStore(s => s.pressingImages);
  const imageMeta = useChatStore(s => s.pendingImageMeta);
  const removePendingImage = useChatStore(s => s.removePendingImage);
  const clearPendingImages = useChatStore(s => s.clearPendingImages);
  const pendingFiles = useChatStore(s => s.pendingFiles);
  const removePendingFile = useChatStore(s => s.removePendingFile);
  const clearPendingFiles = useChatStore(s => s.clearPendingFiles);
  const uploadedFiles = useMemo(
    () => pendingFiles.flatMap(f => (f.status === 'done' && f.uploaded ? [f.uploaded] : [])),
    [pendingFiles],
  );
  const filesUploading = pendingFiles.some(f => f.status === 'uploading');
  // The active chat's queue (dsh's inbox, see SYSTEM: chat-message-queue),
  // minus the chip of the idle send this tab is opening — its optimistic
  // bubble already shows it, so a stale-idle judgement never shows a message
  // twice.
  const inboxQueue = useChatStore(s => s.inboxQueue);
  const liveSend = useChatStore(s => s.liveSend);
  const queued = useMemo(() => (liveSend
    ? inboxQueue.filter(m => (liveSend.messageId ? m.id !== liveSend.messageId : m.text !== liveSend.text))
    : inboxQueue), [inboxQueue, liveSend]);
  const discardQueued = useChatStore(s => s.discardQueued);

  useEffect(() => {
    if (pendingInputFocus) {
      // WHY: a PIN-locked page owns focus — the chat's auto-focus (mount, async
      // session load) lands after PageLockScreen's, so typing went into the chat.
      const ta = textareaRef.current;
      if (ta && !document.querySelector('[data-page-lock-screen]')) {
        ta.focus();
        // WHY: a freshly mounted textarea focuses with the caret at 0 — a quote
        // appended while the chat tab was closed left the caret before it.
        ta.setSelectionRange(ta.value.length, ta.value.length);
      }
      useChatStore.setState({ pendingInputFocus: false });
    }
  }, [pendingInputFocus]);

  // MessageList owns the scroll container; it reports when auto-follow is off and
  // listens for the jump — see 'chat-scrolled-away' / 'chat-scroll-to-bottom'.
  const [scrolledAway, setScrolledAway] = useState(false);
  useEffect(() => {
    const handler = ({ away }: { away: boolean }) => setScrolledAway(away);
    on('chat-scrolled-away', handler);
    return () => off('chat-scrolled-away', handler);
  }, []);
  const scrollToBottom = useCallback(() => emit('chat-scroll-to-bottom'), []);
  const models = useChatStore(s => s.models);
  // WHY: vision-capable model ids (backend-derived) drive the EyeOff badge (shown on models NOT in this set).
  const visionModels = useChatStore(s => s.visionModels);
  const contextWindows = useChatStore(s => s.contextWindows);
  // WHY: per-model reasoning capability (backend-projected from the gateway's
  // /v1/capabilities) drives the effort dropdown. {} → no dropdown (feature
  // absence, not degradation).
  const reasoning = useChatStore(s => s.reasoning);
  const defaultModel = useChatStore(s => s.defaultModel);
  const modelPickerOpen = useChatStore(s => s.modelPickerOpen);
  const setModelPickerOpen = useChatStore(s => s.setModelPickerOpen);
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
  const ghostUnindexed = useChatStore(s => s.ghostUnindexed);
  const setChatUnindexed = useChatStore(s => s.setChatUnindexed);
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
  // through the ONE projection — session materialization and the agent target
  // follow the doc scope.
  const refOpenMode = useUIStore(s => readRefOpenMode(s.documents[currentDocId ?? ''], s.compactLayout));
  const compactLayout = useUIStore(s => s.compactLayout);
  const currentReference = refIsScope(refOpenMode) ? rawReference : null;

  // Red = the persona asked for memory but its settings turned it off (the
  // tooltip says why).
  const memoryError = activeSession?.memory?.error;
  const unindexed = activeSession ? !!activeSession.unindexed : ghostUnindexed;

  const activeSystemPromptId = activeSession?.system_prompt_id ?? null;
  const effectiveSystemPromptId = activeSession ? activeSystemPromptId : ghostSystemPromptId;
  const documents = useAppStore(s => s.documents);
  // ARCH: a persona is
  // any DIRECT child document of the Personas folder (the reserved doc whose
  // system_role === 'system_prompt'), identified by parent_id — NOT by a role
  // tag. Plain docs (no is_system/role) added there are selectable personas. The
  // legacy project prompts-folder picker (projects.system_prompts_doc_id) is
  // removed. Personas ≡ system prompts: chat_sessions.system_prompt_id is the
  // selected persona doc id. (Derived state lives in useSystemPrompts.)
  const { systemPrompts, activeSystemPromptTitle } = useSystemPrompts(documents, effectiveSystemPromptId);
  const memoryPersonas = useMemoryPersonas(projectId, systemPrompts);
  // The trigger shows Brain for a memory-ON persona, ScrollText otherwise — only the icon differs.
  const PersonaIcon = effectiveSystemPromptId && memoryPersonas.has(effectiveSystemPromptId) ? Brain : ScrollText;

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

  // WHY: the shared attachment budget over the turn's PRESSED images (source =
  // GET /chat/models → maxAttachmentMb). Send is disabled and an in-composer error
  // is shown while over the limit, so the user gets explicit feedback instead of a
  // silent 413 at send time.
  const { projectedAttachmentBytes, attachmentsOverLimit } = useAttachmentBudget(maxAttachmentMb, images);

  // INVARIANT: a selected persona ALONE permits an  Why: a persona is a complete instruction with doc metadata attached, so a persona-scoped turn may send an empty body; without a persona an empty send is blocked.
  // empty message — a persona is a complete instruction and the working
  // document's metadata is always attached to a doc-scoped turn. The legacy
  // `&& hasContext` clause is dropped. Why: the common flow is "pick a persona
  // + an open document"; requiring extra attached content would block it. Pure
  // logic lives in chat/send-gate.ts (unit-tested).
  const canSend = computeCanSend({
    text,
    imagesCount: images.length,
    filesCount: uploadedFiles.length,
    filesUploading,
    systemPromptId: effectiveSystemPromptId,
    overLimit: attachmentsOverLimit,
    imagesPressing: pressingImages > 0,
  });

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
        unindexed: ghost.ghostUnindexed,
        // The agent target is ALWAYS the open entity
        // (currentReference ?? currentDocument), never the source chat's
        // target_doc_id.
        targetDocId,
      });
      if (!created) return;
    }
    const imgs = images.length > 0 ? images : undefined;
    // Only uploaded files ride the send; a failed chip leaves with the clear.
    const files = uploadedFiles.length > 0 ? uploadedFiles : undefined;
    // A busy chat queues the whole message in dsh, images and files included —
    // the composer is consumed either way.
    sendMessage(text.trim(), imgs, files);
    setDraft('');
    clearPendingImages();
    clearPendingFiles();
  }, [canSend, activeSessionId, projectId, currentDocId, currentReference, text, images, uploadedFiles, createSession, sendMessage, clearPendingImages, clearPendingFiles, setDraft]);
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData.items;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) void attachImage(file, chatAttachSurface);
      }
    }
  }, []);


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

  // "Analyse in a new chat": open a fresh ghost,
  // then prefill its composer with a request naming THIS chat by title +
  // chat_id, so the agent reads it via read_my_chat without searching. Why
  // capture title/id BEFORE startGhostChat: it clears activeSessionId — after
  // it the session row is no longer resolvable. appendDraft, never setDraft:
  // the draft is ONE shared field across chats and appendDraft is the
  // append-never-overwrite rule (see its INVARIANT in misc-slice) — a setDraft
  // prefill would erase the unsent text of the chat being left. The unindexed
  // guard repeats the button's `disabled` because a programmatic click on a
  // disabled button still reaches onClick. Plain handler, not useCallback: it
  // uses `t`, which is a new function every render (i18n rule — never in a
  // callback dep array).
  const handleAnalyseInNewChat = () => {
    if (!activeSession || activeSession.unindexed) return;
    const title = activeSession.title || t('newChat');
    const id = activeSession.session_id;
    startGhostChat();
    appendDraft(useChatStore.getState, t('analyseChatPrompt', { title, id }));
  };

  const handleModelSelect = useCallback((model: string) => {
    handleModelChange(model);
    textareaRef.current?.focus();
  }, [handleModelChange]);

  // AI-only slots forwarded to the shared ChatComposer. The composer owns the
  // resize handle, textarea fill, attachment chips, mic, and send/stop buttons.
  // WHY one row for ghost and chat alike: Persona and Hide keep their positions
  // across ghost → chat; Analyse appears at the END once there is a chat to
  // analyse, so nothing shifts under the cursor.
  const topControls = (
    <>
      <div className="flex justify-between items-center gap-3 mb-px flex-wrap">
        <div className="flex items-center gap-1">
          {/* The selected persona IS the chat's memory binding; a Brain marks a
              persona with memory ON, in the list and on the trigger. */}
          <Dropdown
            value={effectiveSystemPromptId ?? ''}
            options={[
              { value: '', label: t('chatSystemPromptNone') },
              ...systemPrompts.map(p => ({
                value: p.document_id,
                label: p.title,
                leading: memoryPersonas.has(p.document_id)
                  ? <Brain size={12} aria-label={t('chatPersonaMemoryMark')} />
                  : undefined,
              })),
            ]}
            onSelect={handlePromptSelect}
            icon={<PersonaIcon size={14} className={memoryError ? 'text-red' : undefined} />}
            triggerLabel={activeSystemPromptTitle ?? t('chatSystemPromptNone')}
            title={memoryError ? t('chatMemoryOffNotice', { error: memoryError }) : undefined}
            disabled={isStreaming}
            placement="bottom"
          />
          {/* WHY: operator ruling — the icon is the state: Eye = visible to recall,
              EyeOff on the pressed (gray) background = hidden; no green. */}
          <IconButton
            pressed={unindexed}
            title={unindexed ? t('chatUnindexedOn') : t('chatUnindexedOff')}
            onClick={() => { void setChatUnindexed(!unindexed); }}
          >
            {unindexed ? <EyeOff size={14} /> : <Eye size={14} />}
          </IconButton>
          {activeSessionId && (
            <IconButton
              disabled={!!activeSession?.unindexed}
              title={activeSession?.unindexed ? t('analyseChatHiddenHint') : t('analyseInNewChat')}
              onClick={handleAnalyseInNewChat}
            >
              <Sparkles size={14} />
            </IconButton>
          )}
        </div>
        {/* Token-usage gauge: same row as the persona control, RIGHT
            side. AI-chat only (activeSessionId); fresh session shows 0 / cap.
            The cap prefers the session's context_window — the LAST
            context_usage frame's cap (the model's real window per the
            harness projection) — with effectiveCap(...) as the
            pre-first-frame fallback. Hidden on touch devices (.chat-usage-meters). */}
        {activeSessionId && (
          <div className="chat-usage-meters flex items-center gap-3">
            <TokenUsageGauge
              used={activeSession?.context_tokens_used ?? 0}
              cap={activeSession?.context_window ?? effectiveCap(
                contextWindows,
                activeSession?.model ?? displayModel,
              )}
            />
            {/* Prompt-cache hit share of the last turn, beside the gauge. Absent
                until a turn completes with cache accounting (see CacheHitIndicator). */}
            <CacheHitIndicator />
          </div>
        )}
      </div>
    </>
  );

  const warnings = (
    <>
      {attachmentsOverLimit && (
        <div className="mb-2 text-ui-sm text-danger px-2 py-1 border border-danger/40 bg-danger/5">
          {t('attachmentsExceedLimit', {
            used: (projectedAttachmentBytes / (1024 * 1024)).toFixed(1),
            limit: maxAttachmentMb,
          })}
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
          options={[
            ...models.map(m => ({
              value: m,
              label: m,
              // WHY: EyeOff badge marks models WITHOUT vision — the ones the
              // backend's image-stripping gate strips images for. Vision is the
              // common case, so it carries no mark. Same badge reaches the
              // collapsed trigger via options[selectedIndex]?.badge.
              badge: visionModels.includes(m)
                ? undefined
                : <EyeOff size={11} className="opacity-50 shrink-0" aria-label={t('chatModelNoVision')} />,
            })),
            // WHY: a session pinned to a model outside the user's roster (a
            // revoked grant) renders as its OWN disabled option — the Dropdown
            // clamps an unknown value to the first option, which would show a
            // model the turn will not run.
            ...(displayModel && !models.includes(displayModel)
              ? [{ value: displayModel, label: t('chatModelUnavailableOption', { model: displayModel }), disabled: true }]
              : []),
          ]}
          onSelect={handleModelSelect}
          disabled={isStreaming}
          placement="top"
          open={modelPickerOpen}
          onOpenChange={setModelPickerOpen}
        />
      )}
      {/* WHY: on a compact (phone) layout the reasoning picker is hidden so the
          toolbar fits one row; the session keeps its value, it is changed from a
          wide layout. */}
      {reasoningLevels.length > 0 && !compactLayout && (
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
        className="text-text-muted"
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
      placeholder={activeSessionId ? t('chatPlaceholder') : t('chatPlaceholderNew')}
      recording={recording}
      transcribing={transcribing}
      onToggleRecording={toggleRecording}
      onCancelRecording={cancelRecording}
      images={images}
      onRemoveImage={removePendingImage}
      imageMeta={imageMeta}
      pressingImages={pressingImages}
      files={pendingFiles}
      onRemoveFile={removePendingFile}
      queued={queued}
      onRemoveQueued={id => { void discardQueued(id); }}
      onScrollToBottom={scrolledAway ? scrollToBottom : undefined}
      topControls={topControls}
      leftControls={leftControls}
      warnings={warnings}
      textareaRef={textareaRef}
    />
  );
}

