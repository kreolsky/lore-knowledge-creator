/** Messages slice — load, send, edit, delete, fork, regenerate, stop, sibling navigation. */
import type { ChatMessage, RegionRef } from '../../types';
import { deriveUIMode } from '../../types';
import { apiClient, RequestTooLargeError } from '../../api/client';
import { useAppStore } from '../app-store';
import { useUIStore } from '../ui-store';
import { refIsScope } from '../ui-store/documents-slice';
import { t } from '../../i18n';
import { resolveCompletionContext } from '../../chat/context';
import { resolveRegion } from './pending-selection';
import { recordHistoryLoad } from '../../telemetry/perf';
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
 * than appending a duplicate. `selectedSiblings[parent]` is pointed at the temp
 * id so the optimistic message sits on the active path and renders immediately.
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
  // instantly on send. This is the single shared chokepoint for all three send
  // paths (sendMessage / forkAndResend / regenerate), so one edit covers them.
  // The user-message time IS the sort key: the server aggregate scans
  // role='user' only, so this optimistic value matches the backend-recomputed
  // value on reload (no flicker).
  set(s => {
    const activeSessionId = s.activeSessionId;
    return {
      messages: [...s.messages, optimisticMsg],
      selectedSiblings: { ...s.selectedSiblings, [req.parentId ?? ROOT_KEY]: tempId },
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
    const selectedSiblings = { ...s.selectedSiblings };
    for (const [key, val] of Object.entries(selectedSiblings)) {
      if (key === tempId || val === tempId) delete selectedSiblings[key];
    }
    return {
      messages: s.messages.filter(m => m.message_id !== tempId),
      selectedSiblings,
    };
  });
}

import { buildChildrenMap, resolveActivePath, resolveAncestorChain, ROOT_KEY } from './tree';
import { streamCompletion, flushStreaming, emptyStreaming, adoptOpenTurn, hasOpenHarnessTurn } from './streaming';
import { replaceWindowFromRows, mintImageGen, rewindToLineage } from './conversation-feed';
import { loadMessagesFor, patchMessageContent, deleteMessageById } from '../chat-message-crud';
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
}

export async function runCompletion(set: Set, get: Get, opts: RunCompletionOpts): Promise<void> {
  const { activeSessionId } = get();
  if (!activeSessionId) return;

  const contentCtx = resolveCompletionContext();
  const activeSession = get().sessions.find(s => s.session_id === activeSessionId);
  const systemPromptId = activeSession?.system_prompt_id ?? undefined;
  const uiMode = deriveUIMode(activeSession);
  const autoApply = uiMode === 'agent_auto';
  // ARCH: a mid-turn send POSTs unconditionally (the backend's turn lock
  // answers 409; there is no client-side queue). That bystander send owns NO
  // streaming slot — the open turn's slot is foreign, and flushing at THIS
  // turn's catch/finally would kill the live turn's state machine. The
  // registered turn's own finally is driven by the WS terminal frame, not
  // the POST's return.
  const foreignHarnessTurn = get().streaming !== null;
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
  if (!foreignHarnessTurn) {
    set({ streaming: { ...emptyStreaming(), controller: abortController } });
  }

  // Rewind the assembler to the lineage THIS turn extends, before the
  // boundary is seated in streamCompletion: a fork sends on an ancestor
  // chain while the engine still holds the previous lineage's window, and
  // the fresh dsh session's frames would collide with its seqs (append
  // drops already-held seqs — the fork turn would not stream live). A
  // linear chain is a no-op inside. Skipped for the bystander mid-turn
  // send: the open turn's frames are in no range yet, and rewinding would
  // blank the live streaming row.
  if (!foreignHarnessTurn) {
    const lineage = opts.parentId
      ? resolveAncestorChain(get().messages, opts.parentId).map(m => m.message_id)
      : [];
    rewindToLineage(lineage, activeSessionId, set);
  }

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
    const failed = handleSendError(e, opts.errorLabel);
    if (!foreignHarnessTurn) {
      // WHY the guard: a bystander 409 must not mark (or flush) the OPEN
      // turn's streaming slot — the refusal belongs to a turn that never
      // started. Its toast still fired (handleSendError above).
      if (failed) markStreamingFailed(set, get);
    }
    rollbackOptimisticUser(set, opts.optimisticUserId);
  } finally {
    if (!foreignHarnessTurn) {
      set(flushStreaming);
    }
  }
}

/**
 * Surface a completions send failure to the user (no-silent-degradation).
 *
 * - 413 (body over the server cap — usually accumulated history images) → the
 *   explicit "request too large" toast.
 * - AbortError → silent (user-initiated stop).
 * - Any other failure (network 500, stream throw mid-turn) → a generic error
 *   toast.
 *
 * Returns true for a genuine failure (caller marks the truncated bubble unsaved);
 * false for AbortError (user-initiated stop — silent, no failure badge).
 */
function handleSendError(e: unknown, label: string): boolean {
  if (e instanceof RequestTooLargeError) {
    useAppStore.getState().showToast(t('chatRequestTooLarge'), 'error');
    return true;
  }
  if ((e as Error).name === 'AbortError') {
    return false;
  }
  console.error(label, e);
  useAppStore.getState().showToast(t('chatSendFailed'), 'error');
  return true;
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

type MessagesSlice = Pick<
  ChatState,
  | 'loadMessages'
  | 'resyncOpenHarnessTurn'
  | 'sendMessage'
  | 'editMessage'
  | 'deleteMessage'
  | 'forkAndResend'
  | 'regenerate'
  | 'stopGeneration'
  | 'setImageGenPhase'
  | 'completeImageGen'
  | 'failImageGen'
  | 'selectSibling'
  | 'getSiblings'
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
        // The assembler's reload input: the rows' replayed frames replace the
        // engine's whole window (SYSTEM: dsh-conversation) — BEFORE hydration,
        // which drops the rows' `frames` key. The ADOPTION (adoptOpenTurn)
        // rides the same raw rows: an
        // open_turn row on a harness session re-seats the streaming slot and
        // registers the sink, so a reload mid-turn CONTINUES streaming.
        onRows: rows => {
          replaceWindowFromRows(rows, sessionId, set);
          adoptOpenTurn(get, set, sessionId, rows);
        },
        onLoaded: messages => {
          // INVARIANT: a stale terminal must NEVER touch chatScopeLoading — the gate is
          // owned by whichever request still holds the current scope. Why: when leaving a
          // reference for a different document the ChatPanel effect can fire two loads;
          // the superseded one's loadMessages resolves mid-new-load and, if it cleared the
          // gate here, the spinner turned off then the new load turned it on again →
          // double spinner (groovy-skipping-wozniak). The owning request clears it below.
          set({ messages, messagesLoading: false, chatScopeLoading: false, messagesError: false });
          // Tripwire: flag when full-history load crosses the windowing-payoff threshold.
          recordHistoryLoad(sessionId, messages.length);
          // Reload re-render for mid-turn approval: a still-held call has no live
          // card after a reload (the driver parks on the held POST), so the
          // decision store restores the card onto its assistant message.
          void get().fetchPendingVerdicts(sessionId);
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
      const { activeSessionId } = get();
      if (!activeSessionId) return;
      // ARCH: a mid-turn send POSTs unconditionally — the backend's turn lock
      // is the serializer (a real 409, surfaced by handleSendError as a
      // toast); there is no client-side queue-and-coalesce.
      const { messages, selectedSiblings } = get();
      // WHY: Need clean snapshot without streaming substitution — selectActivePath
      // would inject partial streamingContent if called mid-stream.
      const activePath = resolveActivePath(messages, selectedSiblings);
      const parentId = activePath.length > 0 ? activePath[activePath.length - 1].message_id : null;
      const apiMessages = toApiMessages(activePath);
      apiMessages.push({ role: 'user', content, images });

      const optimisticUserId = insertOptimisticUser(set, get, { parentId, content, images });

      await runCompletion(set, get, {
        apiMessages,
        parentId,
        userContent: content,
        userImages: images,
        optimisticUserId,
        errorLabel: 'Chat send error:',
      });
    },

    async editMessage(messageId: string, content: string) {
      // Shared request layer (chat-message-crud) owns PATCH + error toast.
      const updated = await patchMessageContent(messageId, content);
      if (!updated) return;
      set(s => {
        return { messages: s.messages.map(m => m.message_id === messageId ? updated : m) };
      });
    },

    async deleteMessage(messageId: string) {
      // Shared request layer (chat-message-crud) owns DELETE + error toast.
      if (!(await deleteMessageById(messageId))) return;

      // Client-side BFS to collect all descendant IDs
      const toRemove = new Set<string>();
      const queue = [messageId];
      const childrenMap = buildChildrenMap(get().messages);
      while (queue.length > 0) {
        const id = queue.shift()!;
        toRemove.add(id);
        for (const child of (childrenMap[id] ?? [])) {
          queue.push(child.message_id);
        }
      }

      set(s => {
        const messages = s.messages.filter(m => !toRemove.has(m.message_id));
        const selectedSiblings = { ...s.selectedSiblings };
        for (const [parentKey, selectedId] of Object.entries(selectedSiblings)) {
          if (toRemove.has(selectedId) || toRemove.has(parentKey)) {
            delete selectedSiblings[parentKey];
          }
        }
        return { messages, selectedSiblings };
      });
    },

    async forkAndResend(messageId: string, content: string, images?: string[]) {
      if (get().streaming) return;
      const original = get().messages.find(m => m.message_id === messageId);
      if (!original) return;

      const { activeSessionId } = get();
      if (!activeSessionId) return;

      // Build the ancestor chain up to (not including) the forked message — the
      // fork branches off original.parent_id, via the shared resolveAncestorChain.
      const parentId = original.parent_id;
      const chain = parentId ? resolveAncestorChain(get().messages, parentId) : [];
      const apiMessages = toApiMessages(chain);
      apiMessages.push({ role: 'user', content, images });

      const optimisticUserId = insertOptimisticUser(set, get, { parentId, content, images });

      await runCompletion(set, get, {
        apiMessages,
        parentId,
        userContent: content,
        userImages: images,
        optimisticUserId,
        errorLabel: 'Fork error:',
      });
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
      const controller = get().streaming?.controller ?? null;
      if (controller) {
        const sessionId = get().activeSessionId;
        if (sessionId) {
          // A failed cancel leaves a zombie server-side turn; surface it rather
          // than swallow silently (no-silent-degradation; mirrors the send path).
          apiClient.post(`/chat/sessions/${sessionId}/completions/cancel`, {})
            .catch(() => { useAppStore.getState().showToast(t('stopGenerationFailed'), 'error'); });
        }
        controller.abort();
      }
    },

    setImageGenPhase(runId, messageId, phase) {
      // The generate_image phase is TOP-LEVEL
      // (not on streaming) because the detached generation outlives the agent turn,
      // and KEYED BY runId because two calls in one turn are now possible (the tool
      // returns instantly + enqueues) — per-session correlation let the 2nd run
      // overwrite/clear the 1st. No-op when no messageId or runId.
      if (!messageId || !runId) return;
      set(s => {
        if (!phase) {
          // Delete only this run (null phase ⇒ settled). Mutate a copy.
          if (!(runId in (s.imageGen ?? {}))) return s;
          const { [runId]: _drop, ...rest } = s.imageGen ?? {};
          return { imageGen: rest };
        }
        return { imageGen: { ...(s.imageGen ?? {}), [runId]: { messageId, phase } } };
      });
    },

    completeImageGen(runId, messageId, steps) {
      // The detached
      // background generation finished. The server built + persisted the refiner +
      // image chips and shipped them in the done event — stamp them verbatim (single
      // source, no reconstruction drift). DELETE the runId entry FIRST
      // (unconditional), THEN append chips if any: a done carrying no steps must
      // not return early before clearing, or the spinner runs forever.
      set(s => {
        const { [runId]: _drop, ...rest } = s.imageGen ?? {};
        // The settled run mints its `lore/image-gen` card INTO THE TIMELINE
        // (SYSTEM: dsh-conversation) — the same payload the reload re-derives
        // from the row's gen_steps, anchored at the dispatching call.
        // A miss is LOUD (defect D): without this toast the running chip just
        // vanished and nothing landed — the persisted result was invisible
        // until a reload.
        const minted = mintImageGen(runId, steps, get().activeSessionId, set);
        if (!minted) {
          useAppStore.getState().showToast(t('imageGenCardMissed'), 'error');
        }
        return { imageGen: rest };
      });
    },

    failImageGen(runId, error) {
      // no-silent-
      // degradation — a failed background generation surfaces its cause. Delete only
      // THAT run's entry (independent concurrent runs survive) and toast the error.
      // The failure chip is persisted server-side; here we only stop the spinner.
      set(s => {
        if (!(runId in (s.imageGen ?? {}))) return s;
        const { [runId]: _drop, ...rest } = s.imageGen ?? {};
        return { imageGen: rest };
      });
      useAppStore.getState().showToast(t('imageGenerationFailed', { error }), 'error');
    },

    selectSibling(parentId: string, messageId: string) {
      set(s => ({
        selectedSiblings: { ...s.selectedSiblings, [parentId]: messageId },
      }));
    },

    getSiblings(parentId: string | null): ChatMessage[] {
      const key = parentId ?? ROOT_KEY;
      const children = buildChildrenMap(get().messages)[key] ?? [];
      return children;
    },
  };
}
