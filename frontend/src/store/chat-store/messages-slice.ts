/** Messages slice — load, send, edit, delete, fork, regenerate, stop. */
import type { ChatMessage, ChatSession, RegionRef } from '../../types';
import { deriveUIMode } from '../../types';
import { apiClient, HttpError, RequestTooLargeError } from '../../api/client';
import type { ForbiddenError } from '../../api/client';
import { useAppStore } from '../app-store';
import { useUIStore } from '../ui-store';
import { refIsScope } from '../ui-store/documents-slice';
import { t } from '../../i18n';
import { resolveCompletionContext } from '../../chat/context';
import { resolveRegion } from './pending-selection';
import type { ChatState, Set, Get } from './types';

/** Shape of an optimistic-insert request shared by all three send paths. */
interface OptimisticInsert {
  parentId: string | null;
  content: string;
  images?: string[];
}

/**
 * Insert a temp user message so the sender's bubble appears instantly, before
 * the upload completes. Returns the temp id to pass as `optimisticUserId` and to
 * roll back on failure.
 *
 * WHY: The temp id is prefixed `temp-` and reconciled to the server's real id
 * by the `ids` frame handler (streaming.ts) — it maps the message in place rather
 * than appending a duplicate. The freshest-lineage walk picks it up on its own
 * (it is the newest row), so no sibling selection is written — a branch is its
 * own session and the AI path holds no selections.
 */
function insertOptimisticUser(set: Set, get: Get, req: OptimisticInsert): string {
  const tempId = `temp-${uuid()}`;
  // WHY: stamp the current user's id/name on the optimistic bubble so
  // the nickname renders immediately — not "unknown" until reload. Notes get this
  // for free (server-serialized message); AI chat built the bubble locally without
  // author fields. Synchronous read (Zustand); currentUser is non-null when authed.
  const currentUser = useAppStore.getState().currentUser;
  const nowIso = new Date().toISOString();
  const optimisticMsg: ChatMessage = {
    message_id: tempId,
    chat_id: get().activeSessionId ?? '',
    parent_id: req.parentId,
    role: 'user',
    content: req.content,
    images: req.images,
    created_at: nowIso,
    ...(currentUser ? { author_id: currentUser.user_id, author_name: currentUser.name } : {}),
  };
  // WHY: bump the active session's
  // last_message_at in the SAME set() so the card date + list position refresh
  // instantly on send. This is the single shared chokepoint for the send paths
  // (sendMessage — forkAndResend/regenerate route through it now), so one edit
  // covers them. The user-message time IS the sort key: the server aggregate
  // scans role='user' only, so this optimistic value matches the
  // backend-recomputed value on reload (no flicker).
  set(s => {
    const activeSessionId = s.activeSessionId;
    return {
      messages: [...s.messages, optimisticMsg],
      sessions: activeSessionId
        ? s.sessions.map(ss =>
          ss.session_id === activeSessionId ? { ...ss, last_message_at: nowIso } : ss,
        )
        : s.sessions,
    };
  });
  return tempId;
}

/**
 * Remove a temp user message if it was never acknowledged by the server.
 * Reconciliation in streaming.ts renames the temp id to the real id, so its
 * continued presence in `messages` means the request failed before `ids`
 * arrived — the optimistic bubble must be pulled to avoid a phantom message.
 *
 * INVARIANT: no silent degradation — a failed send never leaves a phantom
 * optimistic bubble visible as if it succeeded. Why: showing an unsent message
 * as sent violates the "never show stale content as current" principle.
 */
function rollbackOptimisticUser(set: Set, tempId: string): void {
  set(s => {
    if (!s.messages.some(m => m.message_id === tempId)) return {};
    return { messages: s.messages.filter(m => m.message_id !== tempId) };
  });
}

import { resolveActivePath, resolveAncestorChain } from './tree';
import { withThreadRow } from './branches-slice';
import { appendDraft } from './misc-slice';
import { streamCompletion, flushStreaming, emptyStreaming, hasOpenHarnessTurn, markHarnessTurnAborted } from './streaming';
import { rewindToLineage } from './conversation-feed';
import { seatSessionRows, afterSessionRowsCommitted } from './session-rows';
import { loadMessagesFor, patchMessageContent } from '../chat-message-crud';
import { uuid } from '../../utils/uuid';

/** Map a chain of stored messages to the wire api-message shape. */
function toApiMessages(chain: ChatMessage[]): Array<{ role: string; content: string; images?: string[] }> {
  return chain.map(m => ({ role: m.role, content: m.content, images: m.images }));
}

/**
 * Shared send/stream entry for all three send paths.
 *
 * WHY: sendMessage / forkAndResend / regenerate each compute their `apiMessages`
 * + `parentId` (via resolveActivePath / resolveAncestorChain) and the optimistic
 * user message, then delegate here for the identical streaming lifecycle: context
 * resolution, system-prompt/auto-apply flags, streamCompletion, error
 * surfacing, rollback, and flush. Collapses ~150 LOC of
 * copy-pasted streaming setup.
 */
interface RunCompletionOpts {
  apiMessages: Array<{ role: string; content: string; images?: string[] }>;
  parentId: string | null;
  userContent: string;
  userImages?: string[];
  optimisticUserId: string;
  errorLabel: string;
  // The turn's parent-tail contract: a 409 "not
  // the tail" on the FIRST attempt is retriable — runCompletion rolls the
  // optimistic bubble back and rethrows TailConflictSignal so sendMessage can
  // re-read the branch and retry ONCE. On the retry (conflictFinal) the same
  // 409 falls through to handleSendError's EXPLICIT toast (no silent
  // degradation, no generic wording).
  conflictFinal?: boolean;
  // sendMessage only (the queue flush rides sendMessage, so it shares this):
  // on a genuine failure the send's own text goes back to the composer.
  // forkAndResend/regenerate do not pass it — their text lives on in the
  // edit box / the existing message.
  restoreDraftOnFail?: boolean;
}

/** Whether the server refused the turn with the non-tail-parent 409 (a stale
 * client view — the branch moved on), as opposed to the turn-lock 409 ("a
 * turn is already in progress"). Keyed on the detail marker, not the bare
 * status; the detail may be any text shape (or absent), so it is guarded. */
function isTailConflict(e: unknown): boolean {
  return (
    e instanceof HttpError
    && e.status === 409
    && typeof e.detail === 'string'
    && e.detail.includes('not the tail')
  );
}

/** Internal sentinel: a retriable tail conflict already rolled back by
 * runCompletion — sendMessage catches it, re-reads, retries once. */
class TailConflictSignal extends Error {
  constructor() { super('tail conflict'); this.name = 'TailConflictSignal'; }
}

export async function runCompletion(set: Set, get: Get, opts: RunCompletionOpts): Promise<void> {
  const { activeSessionId } = get();
  if (!activeSessionId) return;

  const contentCtx = resolveCompletionContext();
  const activeSession = get().sessions.find(s => s.session_id === activeSessionId);
  const systemPromptId = activeSession?.system_prompt_id ?? undefined;
  const uiMode = deriveUIMode(activeSession);
  const autoApply = uiMode === 'agent_auto';
  // ARCH: runCompletion only ever starts on an idle slot — sendMessage routes a
  // mid-turn send to the per-session queue (SYSTEM: chat-message-queue), and
  // forkAndResend/regenerate refuse while streaming. The turn's end is driven by
  // the WS terminal frame, not the POST's return; the backend's turn lock (409)
  // stays as the cross-tab serializer.
  // see SYSTEM: selection-region-agent — resolve the pinned region (frontend-owned) into
  // the wire shape for the prompt hint. has_region forces confirm server-side; the
  // region text is injected as a "# Pinned fragment" block. When the region can't be
  // resolved (cross-device / lost anchor), warn the user — the apply gate will still
  // fail-closed, but the agent can read/propose.
  let region: RegionRef | null = null;
  if (activeSession?.has_region) {
    const res = resolveRegion(activeSessionId);
    if (res.status === 'ok') {
      region = res.region;
    } else if (res.status === 'lost') {
      // Anchor destroyed by a wholesale restore/import — auto-unpin (frees the agent
      // to work on the whole doc) and tell the user, instead of a stale prompt hint.
      void get().regionLost(activeSessionId);
    } else {
      // offline (no live ydoc yet) or none — warn; the apply gate still fails closed.
      useAppStore.getState().showToast(t('regionCollapsedWarning'), 'warning');
    }
  }

  // The live open document (central panel) — resolved synchronously at send time.
  // Per-turn + ephemeral: it surfaces "what's open right now" to the agent WITHOUT
  // repointing the session pin. Priority mirrors the ghost-base (currentReference
  // over currentDocument, both through the scope projection: a panel-previewed ref
  // is not "open" as the scope — the center shows the document). A plain read —
  // no reactivity/dep-array concern.
  const appState = useAppStore.getState();
  const sendMode = useUIStore.getState().getRefOpenMode(appState.currentDocument?.document_id ?? '');
  const scopeRefId = refIsScope(sendMode) ? appState.currentReference?.reference_id : undefined;
  const openDocId =
    scopeRefId ?? appState.currentDocument?.document_id ?? null;

  const abortController = new AbortController();
  // The streaming state machine is one object; idle ⟺ streaming === null.
  // The slot shows THIS chat's turn (streaming.sessionId — the INVARIANT in
  // types.ts); the ownership check in the catch/finally below keeps this run's
  // flush from wiping a slot a leave-and-return re-seated for the same chat.
  set({ streaming: { ...emptyStreaming(activeSessionId), controller: abortController } });

  // Rewind the assembler to the lineage THIS turn extends, before the
  // boundary is seated in streamCompletion: a fork sends on an ancestor
  // chain while the engine still holds the previous lineage's window, and
  // the fresh dsh session's frames would collide with its seqs (append
  // drops already-held seqs — the fork turn would not stream live). A
  // linear chain is a no-op inside.
  const lineage = opts.parentId
    ? resolveAncestorChain(get().messages, opts.parentId).map(m => m.message_id)
    : [];
  rewindToLineage(lineage, activeSessionId, set);

  try {
    await streamCompletion(get, set, {
      sessionId: activeSessionId,
      body: {
        messages: opts.apiMessages,
        parent_id: opts.parentId,
        ...contentCtx,
        ...(systemPromptId ? { system_prompt_id: systemPromptId } : {}),
        ...(autoApply ? { auto_apply: true } : {}),
        ...(region ? { region } : {}),
        ...(openDocId ? { open_doc_id: openDocId } : {}),
      },
      signal: abortController.signal,
      userParentId: opts.parentId,
      userContent: opts.userContent,
      userImages: opts.userImages,
      optimisticUserId: opts.optimisticUserId,
    });
  } catch (e) {
    if (isTailConflict(e) && !opts.conflictFinal) {
      // Retriable: pull the optimistic bubble and hand the conflict to
      // sendMessage's re-read + ONE retry (the plan's contract). The finally
      // below releases the slot this run owns.
      rollbackOptimisticUser(set, opts.optimisticUserId);
      throw new TailConflictSignal();
    }
    const failed = handleSendError(e, opts.errorLabel, set, get);
    // Slot ownership: this run owns the slot only while ITS controller sits in
    // it — a leave-and-return re-seats a fresh controller (adoptOpenTurn), and
    // whatever slot the shown chat then holds is never this run's to flush.
    if (get().streaming?.controller === abortController) {
      if (failed) markStreamingFailed(set, get);
      rollbackOptimisticUser(set, opts.optimisticUserId);
    }
    if (failed && opts.restoreDraftOnFail && opts.userContent) {
      restoreFailedSend(get, activeSessionId, opts.userContent);
    }
  } finally {
    if (get().streaming?.controller === abortController) {
      set(flushStreaming);
    }
  }
}

// INVARIANT(data-loss): a failed send's text goes back to its OWN chat.
// Why: the bubble was rolled back and the composer/chips cleared at send
// time, so without this the text exists nowhere; the draft is ONE shared
// string, so writing it while another chat is shown sends it into the wrong
// chat. Active chat → the composer, appended after anything typed meanwhile
// (appendDraft — the shared restore rule); otherwise → a queued chip of
// that chat.
function restoreFailedSend(get: Get, sessionId: string, text: string): void {
  if (get().activeSessionId !== sessionId) {
    get().enqueueMessage(sessionId, text);
    return;
  }
  appendDraft(get, text);
}

/**
 * Surface a completions send failure to the user (no-silent-degradation).
 *
 * - 413 (body over the server cap — usually accumulated history images) → the
 *   explicit "request too large" toast.
 * - 403 `model_forbidden` (the turn's model is not granted to this user) → a
 *   toast naming the model, a fresh /models roster, and the picker opened.
 * - AbortError → silent (user-initiated stop).
 * - Any other failure (network 500, stream throw mid-turn) → a generic error
 *   toast.
 *
 * Returns true for a genuine failure (caller marks the truncated bubble unsaved);
 * false for AbortError (user-initiated stop — silent, no failure badge).
 */
function handleSendError(e: unknown, label: string, set: Set, get: Get): boolean {
  if (e instanceof RequestTooLargeError) {
    useAppStore.getState().showToast(t('chatRequestTooLarge'), 'error');
    return true;
  }
  if (isTailConflict(e)) {
    // The retried turn STILL conflicts — the branch keeps moving under this
    // client. Explicit wording (not the generic failure toast): the view is
    // refreshed, the send is in the composer's hands again.
    useAppStore.getState().showToast(t('chatBranchMovedOn'), 'error');
    return true;
  }
  const refused = modelForbidden(e);
  if (refused !== null) {
    // WHY: a refused model is never silently swapped for another one — the
    // user is told which model and picks the next one themself; a swap would
    // answer with a model the user did not choose, under the name of the one
    // they did.
    useAppStore.getState().showToast(t('chatModelForbidden', { model: refused }), 'error');
    // The cached roster still lists the revoked model — refetch before the
    // picker opens on it.
    set({ modelsLoaded: false, modelPickerOpen: true });
    void get().loadModels();
    return true;
  }
  if ((e as Error).name === 'AbortError') {
    return false;
  }
  console.error(label, e);
  useAppStore.getState().showToast(t('chatSendFailed'), 'error');
  return true;
}

/** The refused model id of a 403 `{code: 'model_forbidden', model}`, else null. */
function modelForbidden(e: unknown): string | null {
  // Matched by name, like the AbortError check in handleSendError.
  if ((e as Error | null)?.name !== 'ForbiddenError') return null;
  const detail = (e as ForbiddenError).detail as { code?: unknown; model?: unknown } | null | undefined;
  if (detail?.code !== 'model_forbidden') return null;
  return typeof detail.model === 'string' ? detail.model : '';
}

/**
 * Mark the in-flight assistant message `unsaved` after a stream failure that
 * occurred AFTER the `ids` event (so a truncated bubble is visibly distinct from
 * a complete answer).
 *
 * INVARIANT: no silent degradation — a failed stream never finalizes a partial
 * answer as if it succeeded. Why: showing a truncated response as complete
 * violates "never show stale content as current".
 */
function markStreamingFailed(set: Set, get: Get): void {
  const failedId = get().streaming?.messageId ?? null;
  if (!failedId) return;
  if (!get().messages.some(m => m.message_id === failedId)) return;
  set(s => ({
    messages: s.messages.map(m =>
      m.message_id === failedId ? { ...m, unsaved: true } : m,
    ),
  }));
}

/** One send attempt: guards → path derivation → optimistic insert → the
 * shared completion run. All errors are handled inside runCompletion EXCEPT
 * the retriable tail conflict, which surfaces here as 'tail-conflict' for
 * sendMessage's single re-read + retry. */
async function _sendOnce(
  set: Set, get: Get, content: string, images: string[] | undefined,
  conflictFinal: boolean,
): Promise<'sent' | 'queued' | 'tail-conflict'> {
  const { activeSessionId, streaming } = get();
  if (!activeSessionId) return 'queued';
  // INVARIANT(data-loss): a POST fires only for a chat with no open turn
  // OF ITS OWN. Why: the backend locks per session (409), and the slot
  // covers only the ACTIVE chat — another chat's turn must never queue
  // this one (the queued text may never send). The registration clause
  // covers the return window (the slot is null between setActiveSession
  // and the reload's re-adoption). The guard lives in the store (not the
  // UI) so MicButton transcription + hotkeys can't bypass it; one
  // ordinary sendMessage fires later on flush. Images stay in the
  // composer (attachment-budget merge is a separate problem).
  if (streaming || hasOpenHarnessTurn(activeSessionId)) {
    get().enqueueMessage(activeSessionId, content);
    return 'queued';
  }
  // Fresh derivation per attempt: the RETRY must parent on the re-read
  // branch's tail, not the stale view that conflicted. The walk takes the
  // freshest lineage (a branch session's rows are a linear chain).
  const activePath = resolveActivePath(get().messages);
  const parentId = activePath.length > 0 ? activePath[activePath.length - 1].message_id : null;
  const apiMessages = toApiMessages(activePath);
  apiMessages.push({ role: 'user', content, images });

  const optimisticUserId = insertOptimisticUser(set, get, { parentId, content, images });

  try {
    await runCompletion(set, get, {
      apiMessages,
      parentId,
      userContent: content,
      userImages: images,
      optimisticUserId,
      errorLabel: 'Chat send error:',
      conflictFinal,
      restoreDraftOnFail: true,
    });
    return 'sent';
  } catch (e) {
    if (e instanceof TailConflictSignal) return 'tail-conflict';
    // Everything else is handled inside runCompletion (toast + rollback +
    // draft restore); the attempt is done.
    return 'sent';
  }
}

type MessagesSlice = Pick<
  ChatState,
  | 'loadMessages'
  | 'resyncOpenHarnessTurn'
  | 'sendMessage'
  | 'editMessage'
  | 'forkAndResend'
  | 'regenerate'
  | 'stopGeneration'
  | 'rewindTo'
>;

export function createMessagesSlice(set: Set, get: Get): MessagesSlice {
  return {
    async loadMessages(sessionId: string) {
      set({ messagesLoading: true, messagesError: false });
      // Shared request layer (chat-message-crud) owns GET + staleness guard +
      // error toast; the chat-scope terminals (gate ownership, tripwire)
      // are the scope discriminator here.
      await loadMessagesFor(sessionId, {
        activeSessionId: () => get().activeSessionId,
        onRows: rows => seatSessionRows(get, set, sessionId, rows),
        onLoaded: messages => {
          // INVARIANT: a stale terminal must NEVER touch chatScopeLoading — the gate is
          // owned by whichever request still holds the current scope. Why: when leaving a
          // reference for a different document the ChatPanel effect can fire two loads;
          // the superseded one's loadMessages resolves mid-new-load and, if it cleared the
          // gate here, the spinner turned off then the new load turned it on again →
          // double spinner (groovy-skipping-wozniak). The owning request clears it below.
          set({ messages, messagesLoading: false, chatScopeLoading: false, messagesError: false });
          afterSessionRowsCommitted(get, sessionId, messages.length);
        },
        onLoadError: () => {
          // INVARIANT: clear the gate only when THIS request still owns the scope. Why: a
          // superseded loadMessages rejecting must not blank the spinner the new scope is
          // driving (gate-ownership; groovy-skipping-wozniak).
          set({ messagesLoading: false, chatScopeLoading: false, messagesError: true });
        },
      });
    },

    async resyncOpenHarnessTurn() {
      // The browser-WS-gap recovery. A project-WS reconnect means chat_frame envelopes were
      // LOST for any open harness turn (the fan-out sends to the owner's
      // LIVE sockets; nothing replays to a socket that was not there). The
      // reload's replay + re-adoption rebuilds the window; the kept
      // registration's settle promise survives (adoptOpenTurn's gap twins).
      const sessionId = get().activeSessionId;
      if (!sessionId || !hasOpenHarnessTurn(sessionId)) return;
      await get().loadMessages(sessionId);
    },

    async sendMessage(content: string, images?: string[]) {
      // INVARIANT: This function relies on Zustand's synchronous `set()` — it reads  Why: sendMessage reads activeSessionId+messages via get() then sets streaming via set() in the same microtask; downstream frame consumers rely on that synchronous handoff.
      // `activeSessionId` and `messages` via `get()` in the same microtask, then
      // sets the `streaming` object via `set()`. All downstream consumers (frame
      // handlers, UI components) assume the write is immediately visible. If
      // Zustand ever makes `set()` async, the streaming state machine breaks.
      //
      // The linear-turn contract: the turn's
      // parent must name the branch's live tail. A 409 "not the tail" means
      // another turn landed first — re-read the branch and retry ONCE; a
      // second conflict shows an explicit toast (runCompletion's
      // conflictFinal arm).
      const outcome = await _sendOnce(set, get, content, images, false);
      if (outcome !== 'tail-conflict') return;
      const { activeSessionId } = get();
      if (!activeSessionId) return;
      await get().loadMessages(activeSessionId);
      if (get().activeSessionId !== activeSessionId) return;
      await _sendOnce(set, get, content, images, true);
    },

    async editMessage(messageId: string, content: string) {
      // Shared request layer (chat-message-crud) owns PATCH + error toast.
      const updated = await patchMessageContent(messageId, content);
      if (!updated) return;
      set(s => {
        return { messages: s.messages.map(m => m.message_id === messageId ? updated : m) };
      });
    },

    async forkAndResend(messageId: string, content: string, images?: string[]) {
      if (get().streaming) return;
      const original = get().messages.find(m => m.message_id === messageId);
      const { activeSessionId } = get();
      if (!original || !activeSessionId) return;
      // The first message of an AI chat is immutable (operator ruling): there
      // is no root-level fork. The UI hides the edit button on it; the guard
      // is defense-in-depth.
      if (!original.parent_id) return;

      // ARCH: edit-and-resend = create a branch
      // after the message's parent ⇒ open it ⇒ send the edited text as an
      // ordinary linear turn. The branch COPIES the shared prefix; the source
      // session is never re-pointed.
      let branch: ChatSession;
      try {
        branch = await apiClient.post(
          `/chat/sessions/${activeSessionId}/branches`,
          { after_message_id: original.parent_id },
        );
      } catch {
        useAppStore.getState().showToast(t('chatBranchCreateFailed'), 'error');
        return;
      }
      set(s => ({ sessions: withThreadRow(s.sessions, branch) }));
      get().openBranch(branch.session_id);
      // The send parents on the branch's TAIL (the copied prefix's end) —
      // the rows must be seated first (the queue-flush switch+load pattern).
      await get().loadMessages(branch.session_id);
      if (get().activeSessionId !== branch.session_id) return;
      await get().sendMessage(content, images);
    },

    async regenerate(messageId: string) {
      if (get().streaming) return;
      const msg = get().messages.find(m => m.message_id === messageId);
      if (!msg || msg.role !== 'assistant') return;

      const { activeSessionId } = get();
      if (!activeSessionId) return;

      // Regenerate gate: agent chats do NOT support regenerate yet (re-running an
      // assistant turn needs its own leaf-sync wiring — out of scope for the
      // fork feature). Note-chats still allow it. This INTENTIONALLY differs
      // from forkAndResend (agent fork is allowed) — separate rationales, do not
      // merge. The `sessionForGate && !is_note` form (not `!?.is_note`) avoids
      // tripping the gate on an absent session row (load race): a bare
      // `!?.is_note` flips true when the row isn't loaded yet.
      const sessionForGate = get().sessions.find(s => s.session_id === activeSessionId);
      if (sessionForGate && !sessionForGate.is_note) {
        useAppStore.getState().showToast(t('agentModeNoForkRegenerate'), 'error');
        return;
      }

      // The new user+assistant pair is a sibling of the original assistant — its
      // parent is the grandparent (the assistant's parent's parent). The
      // ancestor chain (shared resolver) gives both the conversation history and
      // the user message content/images (the chain tail is the user message).
      const userMsgId = msg.parent_id;
      if (!userMsgId) return;
      const chain = resolveAncestorChain(get().messages, userMsgId);
      if (chain.length === 0) return;

      const userMsg = chain[chain.length - 1];
      const userParent = msg.parent_id ? get().messages.find(m => m.message_id === msg.parent_id) : null;
      const userParentId = userParent?.parent_id ?? null;

      const apiMessages = toApiMessages(chain);
      const optimisticUserId = insertOptimisticUser(set, get, {
        parentId: userParentId, content: userMsg.content, images: userMsg.images,
      });

      await runCompletion(set, get, {
        apiMessages,
        parentId: userParentId,
        userContent: userMsg.content,
        userImages: userMsg.images,
        optimisticUserId,
        errorLabel: 'Regenerate error:',
      });
    },

    stopGeneration() {
      const st = get().streaming;
      const controller = st?.controller ?? null;
      if (controller) {
        const sessionId = st?.sessionId ?? get().activeSessionId;
        if (sessionId) {
          // Stamp the registration aborted BEFORE the abort (the end facts
          // live on the registration — see TurnEndFacts): a trailing `error`
          // frame of this deliberate stop must not surface as a failure, and
          // the terminal must read the turn as 'aborted'.
          markHarnessTurnAborted(sessionId);
          // A failed cancel leaves a zombie server-side turn; surface it rather
          // than swallow silently (no-silent-degradation; mirrors the send path).
          apiClient.post(`/chat/sessions/${sessionId}/completions/cancel`, {})
            .catch(() => { useAppStore.getState().showToast(t('stopGenerationFailed'), 'error'); });
        }
        controller.abort();
      }
    },

    async rewindTo(messageId: string) {
      if (get().streaming) return;
      const target = get().messages.find(m => m.message_id === messageId);
      const { activeSessionId } = get();
      if (!target || !activeSessionId) return;
      // The first message is immutable — no rewind before it (the UI hides
      // the button; the guard is defense-in-depth).
      if (!target.parent_id) return;

      // ARCH: rewind = fork a branch after the
      // message's PARENT and open it. The new branch's transcript ends right
      // before the rewound message and the composer owns the next turn; the
      // old line stays in history (the switcher offers it at that fork
      // point). No client-side cut sentinel — the branch IS the cut.
      let branch: ChatSession;
      try {
        branch = await apiClient.post(
          `/chat/sessions/${activeSessionId}/branches`,
          { after_message_id: target.parent_id },
        );
      } catch {
        useAppStore.getState().showToast(t('chatBranchCreateFailed'), 'error');
        return;
      }
      set(s => ({ sessions: withThreadRow(s.sessions, branch) }));
      get().openBranch(branch.session_id);
      set({ pendingInputFocus: true });
    },
  };
}
