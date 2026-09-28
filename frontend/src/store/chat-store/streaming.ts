/** Chat turn transport + flushStreaming reducer.
 *
 * ONE transport: the turn is DRIVER-owned — POST /completions answers JSON
 * and the frames arrive as project-WS chat_frame envelopes, dispatched
 * through createTurnSink into the frame handlers + the assembler feed. */
import type { ChatMessage, ChatSource } from '../../types';
import { apiClient } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import type { ChatState, Set, StreamingState } from './types';
import { ROOT_KEY } from './tree';
import { validateFrame, type Frame } from './frame-validate';
import { registerChatResetHandler } from './reset-registry';
import {
  isFeedFrame, feedFrame, beginTurn, endTurn,
} from './conversation-feed';

/** A wire field is usable as display text only when it is a non-empty string —
 * the unguarded terminal frames render a fallback otherwise (see `error` /
 * `lore/halt`). */
const isNonEmptyStr = (v: unknown): v is string =>
  typeof v === 'string' && v.length > 0;

/** A fresh, idle streaming-state object (no controller). */
export function emptyStreaming(): StreamingState {
  return { messageId: null, content: '', controller: null };
}

// ARCH: a turn emits `sources` MORE THAN ONCE and in ANY order relative to
// `ids`: the manual/explicit context sources (retrieved:false) are streamed first — before
// the `ids` event that creates the assistant message — and each `search_materials`
// agent call later streams its semantic hits (retrieved:true). We accumulate across
// all `sources` events into one deduped list.
// INVARIANT: dedup by id, and a manual entry (retrieved:false) wins over a retrieval
// twin (retrieved:true) for the same id — the user explicitly attached the whole
// document, so it must show without the auto-found chain icon.  Why: dedup by id; a manually-attached entry (retrieved:false) wins over its auto-found twin (retrieved:true) so an explicit attachment shows without the chain icon.
// Why: the backend reorder in 8d16796 put `sources` before `ids`; a handler that
// only attached when streamingMessageId was already set silently dropped the manual
// sources. Order-independent accumulation removes the ordering dependency entirely.
function mergeSources(acc: ChatSource[], incoming: ChatSource[]): ChatSource[] {
  const next = [...acc];
  for (const src of incoming) {
    const i = next.findIndex(s => s.id === src.id);
    if (i === -1) {
      next.push(src);
    } else if (next[i].retrieved !== false && src.retrieved === false) {
      next[i] = src;
    }
  }
  return next;
}

/** Finalize streaming.content into the message, clear state. */
export function flushStreaming(s: ChatState): Partial<ChatState> {
  const st = s.streaming;
  if (!st) return { streaming: null };
  const result: Partial<ChatState> = { streaming: null };
  if (st.messageId && st.content) {
    result.messages = s.messages.map(m => {
      if (m.message_id !== st.messageId) return m;
      const next = { ...m };
      if (st.content) next.content = st.content;
      return next;
    });
  }
  return result;
}

// ─── the ONE per-frame reducer ───────────────────────────────────────────────
//
// ARCH: a held mutating call parks the ask IN THE DRIVER — the card is the
// assembler's `lore/verdict-ask` node, the answer is POST /api/chat/verdicts,
// and the card settles on the held call's tool/result frame. There is no
// `paused` event and no resume: the driver owns the turn until its terminal
// frame.

/** The per-turn facts the frame handlers close over — the turn's opts
 * and the harness registration's context are the same shape. */
export interface TurnFramesCtx {
  sessionId: string;
  userContent: string;
  userParentId: string | null;
  optimisticUserId?: string;
  userImages?: string[];
}

function createTurnSink(
  get: () => ChatState,
  set: Set,
  ctx: TurnFramesCtx,
): { frame: (ev: Frame) => void; accept: (obj: unknown) => Frame | null; warnMalformed: (e: unknown) => void } {
  const opts = ctx;
  let sourcesForMsg: ChatSource[] = [];
  let warnedMalformed = false;  // one-shot per stream (see the drop site below)

  const warnMalformed = (e: unknown): void => {
    console.warn('Malformed chat frame:', e);
    // WHY: a malformed KNOWN frame (or an unparseable line) is a protocol
    // breach, not forward-compat garbage — dropping it silently could lose a
    // card (no-silent-degradation); surface once per turn, never per frame.
    if (!warnedMalformed) {
      warnedMalformed = true;
      useAppStore.getState().showToast(t('chatStreamDegraded'), 'warning');
    }
  };

  // Frame handler map: the per-event-type logic lives in ONE record instead of a
  // long if/else chain. The handlers close over the shared `sourcesForMsg` and
  // are dispatched by `event.type` below. Per-token batching is the FEED's job
  // now (conversation-feed coalesces its publications per animation frame), so
  // no delta accumulator lives here.

  const handlers: Record<string, (ev: any) => void> = {
    ids(ev) {
      // ARCH: the `ids` event carries only message ids, not author info,
      // so the reconciled user message would show "unknown" until reload. Stamp the
      // current user's id/name here too (mirrors insertOptimisticUser).
      const currentUser = useAppStore.getState().currentUser;
      const userMsg: ChatMessage = {
        message_id: ev.user_message_id,
        chat_id: opts.sessionId,
        parent_id: opts.userParentId,
        role: 'user',
        content: opts.userContent,
        images: opts.userImages,
        created_at: new Date().toISOString(),
        ...(currentUser ? { author_id: currentUser.user_id, author_name: currentUser.name } : {}),
      };
      const assistantMsg: ChatMessage = {
        message_id: ev.assistant_message_id,
        chat_id: opts.sessionId,
        parent_id: ev.user_message_id,
        role: 'assistant',
        content: '',
        created_at: new Date().toISOString(),
        // Seed from any `sources` events that arrived before `ids`.
        ...(sourcesForMsg.length > 0 ? { sources: sourcesForMsg } : {}),
      };
      const hasOptimistic = !!opts.optimisticUserId
        && get().messages.some(m => m.message_id === opts.optimisticUserId);
      const baseMessages = hasOptimistic
        ? get().messages.map(m => m.message_id === opts.optimisticUserId ? userMsg : m)
        : [...get().messages, userMsg];
      const newMessages = [...baseMessages, assistantMsg];
      const newSelected = { ...get().selectedSiblings };
      if (hasOptimistic && opts.optimisticUserId) {
        delete newSelected[opts.optimisticUserId];
      }
      newSelected[opts.userParentId ?? ROOT_KEY] = ev.user_message_id;
      newSelected[ev.user_message_id] = ev.assistant_message_id;
      set({
        messages: newMessages,
        selectedSiblings: newSelected,
        streaming: {
          // Preserve the controller created at send time; reset the per-message
          // accumulators for the (possibly resumed) turn.
          ...(get().streaming ?? emptyStreaming()),
          messageId: ev.assistant_message_id,
        },
      });
    },
    error(ev) {
      console.error('Chat stream error:', ev.message);
      // WHY: a trailing `error` frame that arrives
      // while the user has already pressed Stop (the turn's controller is aborted)
      // is part of the deliberate stop, not a failure — skip the inline error + toast.
      // Mirrors the AbortError guard in messages-slice.handleSendError (no failure
      // badge / toast on a user-initiated stop). Why: the backend can emit an error
      // frame as the aborted turn winds down, and surfacing it would falsely report a
      // failure for an action the user chose.
      if (get().streaming?.controller?.signal.aborted) return;
      // APPEND the error notice to the row's content rather than replacing it.
      // Why: an empty-completion error still has the partial work the agent did,
      // and a frameless row (and a reload, where content is the row's own)
      // renders the notice there.
      // INVARIANT: the notice never renders `undefined`. Why: this frame is
      // deliberately not shape-guarded — dropping it would lose the turn's
      // failure notice, a worse silence than the one a guard prevents. The
      // fallback is what makes passing it through degraded-but-HONEST rather
      // than degraded-but-garbled.
      const errMsg = `⚠ ${isNonEmptyStr(ev.message) ? ev.message : t('chatStreamFrameIncomplete')}`;
      const mid = get().streaming?.messageId ?? null;
      // This is a BACKEND-minted frame, not a dsh event, so the assembler
      // builds no node for it — and an assembled turn renders its nodes
      // INSTEAD of `content`, which would leave the appended notice unread on
      // exactly the turns that have a timeline. It also covers a frame arriving
      // BEFORE `ids`, which has no row to write into and does NOT throw: the
      // stream completes normally, so runCompletion's catch never fires.
      // WHY: an `error` frame always reaches the user as a toast — it is the
      // one signal that does not depend on which branch the bubble took.
      useAppStore.getState().showToast(t('chatSendFailed'), 'error');
      set(s => ({
        messages: s.messages.map(m =>
          mid && m.message_id === mid ? { ...m, content: (m.content ?? '') + errMsg } : m,
        ),
      }));
    },
    sources(ev) {
      sourcesForMsg = mergeSources(sourcesForMsg, ev.sources as ChatSource[]);
      // Apply now if the assistant message already exists; otherwise the `ids`
      // handler seeds it from the accumulator (order-independent).
      const mid = get().streaming?.messageId ?? null;
      if (mid) {
        set(s => ({
          messages: s.messages.map(m =>
            m.message_id === mid ? { ...m, sources: sourcesForMsg } : m,
          ),
        }));
      }
    },
    // The dsh_event + lore/* frames have NO handler here: they are the
    // assembler's input — the dispatch below feeds them to conversation-feed
    // before consulting this table, and the components render the published
    // nodes; what they announce rides as a dsh kind or a lore/* mint. What
    // remains here is the LIVE-ONLY work beside the feed.
    // The harness titler's revision: apply it to the chat-list row live (the
    // list acceptance: "without a second request"). The plugin guards the
    // empty/leak payloads at the source, so a frame that arrives carries a
    // title worth showing; the user pin (title_user_set, set by the PATCH
    // rename) still holds here. LIVE-only: a reload reads the title from the
    // row the backend write landed on. It mints no step — the reducer has no
    // arm for the type, which is the same nothing a reload replays.
    session_title(ev) {
      const title = typeof ev.title === 'string' ? ev.title : '';
      if (!title) return;
      set(s => ({
        sessions: s.sessions.map(ss =>
          ss.session_id === opts.sessionId && !ss.title_user_set
            ? { ...ss, title }
            : ss,
        ),
      }));
    },
    context_warning() { useAppStore.getState().showToast(t('chatContextWarning'), 'warning'); },
    // A live harness signal with no dedicated UI. `model_update` is persisted on
    // the message by the backend reducer, so the frontend renders it from the row
    // on reload; the seat here is documentation, not a guard — a type with NO
    // handler is ignored just as silently (see the dispatch below).
    model_update() { /* persisted server-side; no live UI yet */ },
    // The turn's text, as the BACKEND accumulated it (driver_frames: the row's
    // `content` column is ours, the dsh log holds the trace). The bubble renders
    // the assembler's nodes, so this is not what draws the reply — it is what
    // the chat-list preview, a copy action and a frameless fallback read, and
    // without it they stay empty until a reload re-fetches the row.
    done(ev) {
      const mid = get().streaming?.messageId ?? null;
      if (!mid || !isNonEmptyStr(ev.content)) return;
      const content = ev.content as string;
      set(s => ({
        messages: s.messages.map(m => (m.message_id === mid ? { ...m, content } : m)),
      }));
    },
    // The halt CARD is the assembler's node; the end REASON is not a rendering.
    'lore/halt'() { /* the card rides the feed */ },
    // The compaction mint OUTCOME rides the timeline as `lore/compaction-mint`
    // (the frame is ALSO fed to the assembler — the dispatch below does both).
    // The toast is LIVE-only beside the feed (a reload shows the minted card,
    // and re-toasting it would lie about when the summarize happened).
    'lore/compaction-mint'(ev) {
      const { showToast } = useAppStore.getState();
      if (ev.data?.mintFailed === true) {
        showToast(t('chatCompactionArchiveFailed'), 'warning');
        return;
      }
      showToast(t('chatContextSummarized'), 'info');
    },
    // Per-turn context occupation: one update per turn (not per token) onto the
    // ACTIVE session row in sessions[] — the gauge is session-bound (above the
    // input), and a reload rehydrates the used figure from the persisted row.
    // ev.cap becomes the row's LIVE context_window: the frame's denominator is
    // the harness projection + threaded window (the model's real context
    // length), so it self-corrects a stale /models fallback on every turn.
    context_usage(ev) {
      set(s => ({
        sessions: s.sessions.map(ss =>
          ss.session_id === opts.sessionId
            ? { ...ss, context_tokens_used: ev.used, context_window: ev.cap }
            : ss,
        ),
      }));
    },
  };

  const frame = (event: Frame): void => {
    // The feed FIRST: a verbatim dsh event or a lore mint is the
    // assembler's input (the components render its published nodes); the
    // handler table below is the LIVE-ONLY work beside the feed (toasts,
    // the title row, the context gauge). A feed frame may also carry an
    // arm (lore/compaction-mint's toast).
    if (isFeedFrame(event)) feedFrame(event as Record<string, unknown>, opts.sessionId, set);
    // Handler exceptions are NOT protocol breaches — they propagate to the
    // transport's own error path (the dispatch loop's catch; the WS dispatch
    // lets them throw into the socket handler's console.warn).
    const handler = handlers[event.type];
    if (handler) handler(event);
  };

  const accept = (obj: unknown): Frame | null => {
    let event: Frame | null;
    try {
      event = validateFrame(obj);
    } catch (e) {
      warnMalformed(e);
      return null;
    }
    if (!event) {
      warnMalformed(obj);
      return null;
    }
    frame(event);
    return event;
  };

  return { frame, accept, warnMalformed };
}

// ─── the transport ───────────────────────────────────────────────────────────
//
// ARCH: every chat session's turn is DRIVER-owned. The POST
// /completions answers JSON {accepted, …} and returns while the turn runs; the
// frames ride the PROJECT lifecycle WS as `{type:'chat_frame', session_id,
// frame}` (SYSTEM: chat-fanout) and enter through dispatchChatFrame — the SAME
// sink, the same assembler, the same handlers. streamCompletion
// for a harness session resolves only when the turn ENDS: a terminal frame
// (`done` — the graceful and setup-failure tail — or `turn_closed` — the
// transport terminal the plugin pushes / the channel re-mints) settles it, so
// runCompletion's catch/finally see the SAME turn lifecycle semantics as a
// (the stream end), not the POST's return.
//
// The registration is per CHAT session id (the WS envelope's session_id — the
// fan-out keys it by the chat id even for continuation chats). Frames for a
// session with NO registration are ignored: another tab's turn, or a turn this
// tab never sent (a reload mid-turn adopts nothing — the reload path renders
// the rows; live continuation is the acceptance drive's own step).

interface _HarnessTurn {
  sink: ReturnType<typeof createTurnSink>;
  settled: boolean;
  settle: () => void;
}

const harnessTurns = new Map<string, _HarnessTurn>();

/** Whether THIS tab holds an open harness turn registration for the session —
 * the browser-WS-gap resync's trigger (a reconnect reloads the session when
 * this is true: frames lost in the socket gap recover through the reload's
 * replay + re-adoption). */
export function hasOpenHarnessTurn(sessionId: string): boolean {
  return harnessTurns.has(sessionId);
}

/**
 * Reload-mid-turn ADOPTION.
 *
 * The messages GET marks the row whose turn is still open (`open_turn`,
 * backend _mark_open_turn). For a harness session the reload ADOPTS that
 * turn: the streaming slot re-seats on the open row (it
 * renders streaming, never a settled partial that flips on the next frame)
 * and the WS registration exists, so live frames CONTINUE into the same
 * assembler window the reload just rebuilt.
 *
 * The browser-WS-gap twins ride the same entry point (a reconnect re-runs
 * loadMessages → this):
 * - an EXISTING registration is KEPT — its settle promise is awaited by this
 *   tab's runCompletion, and replacing it would hang that await forever;
 * - a reload that shows the turn ENDED (no open mark) while a registration
 *   still waits closes it: the terminal frame died with the socket, and
 *   nothing else will ever settle it.
 */
export function adoptOpenTurn(
  get: () => ChatState,
  set: Set,
  sessionId: string,
  rows: ReadonlyArray<{ message_id?: string; open_turn?: boolean }>,
): void {
  const reg = harnessTurns.get(sessionId);
  const open = rows.find(r => r.open_turn === true);
  if (!open || typeof open.message_id !== 'string' || !open.message_id) {
    if (reg && !reg.settled) {
      // The turn ended inside our gap: close what the lost terminal cannot.
      // (No registration + no open row: nothing to do — plain reload.)
      endTurn(sessionId, get().streaming?.messageId ?? null, set);
      set(flushStreaming);
      harnessTurns.delete(sessionId);
      reg.settled = true;
      reg.settle();
    }
    return;
  }
  // Seat the slot with a controller: Stop on an adopted turn routes through
  // stopGeneration's cancel POST exactly like a live one.
  set({ streaming: { ...emptyStreaming(), controller: new AbortController(), messageId: open.message_id } });
  // The boundary is the OPEN window's min (replaceWindowFromRows just
  // published it) — the whole open turn renders in the streaming slot.
  beginTurn(sessionId, set, get().turnRanges[open.message_id]?.min);
  if (!reg) {
    let resolveEnd!: () => void;
    const turnEnded = new Promise<void>(r => { resolveEnd = r; });
    harnessTurns.set(sessionId, {
      sink: createTurnSink(get, set, {
        sessionId, userContent: '', userParentId: null,
      }),
      settled: false,
      settle: () => resolveEnd(),
    });
    // Nobody awaits an adopted turn's promise — but the registration MUST
    // carry one so the terminal path (dispatchChatFrame) stays uniform.
    void turnEnded;
  }
}

/** The frame types that END a turn on the WS transport: `done` (minted
 * backend-side for graceful ends and harness setup failures) and
 * `turn_closed` (pushed by the plugin after the turn's last mapped frame —
 * an errored turn mints no done). Whichever arrives first closes the turn;
 * the other is a no-op (the registration is gone). */
const TERMINAL_FRAME_TYPES: ReadonlySet<string> = new Set(['done', 'turn_closed']);

/** One project-WS chat_frame envelope, dispatched into the open harness turn.
 * Project-connection stays transport-dumb: this is the store-side sink. */
export function dispatchChatFrame(
  get: () => ChatState,
  set: Set,
  sessionId: string,
  frame: unknown,
): void {
  const turn = harnessTurns.get(sessionId);
  if (!turn) return;
  let event: Frame | null = null;
  try {
    event = turn.sink.accept(frame);
  } catch (e) {
    // WHY: a handler exception must not kill the socket's dispatch loop — mirror
    // dispatch loop's catch (which surfaces through runCompletion) with a
    // loud console line; the terminal frame still closes the turn.
    console.error('Chat frame handler failed:', e);
  }
  if (event && TERMINAL_FRAME_TYPES.has(event.type)) {
    // The turn's transport end: bind the fed nodes, flush the
    // slot, THEN settle — runCompletion's finally reads a clean slate.
    endTurn(sessionId, get().streaming?.messageId ?? null, set);
    set(flushStreaming);
    harnessTurns.delete(sessionId);
    turn.settled = true;
    turn.settle();
  }
}

async function _harnessCompletion(
  get: () => ChatState,
  set: Set,
  opts: {
    sessionId: string;
    body: Record<string, unknown>;
    signal: AbortSignal;
    userParentId: string | null;
    userContent: string;
    userImages?: string[];
    optimisticUserId?: string;
  },
): Promise<void> {
  const { sessionId } = opts;
  const endpoint = `/chat/sessions/${sessionId}/completions`;
  // The bystander: another harness turn is open in THIS tab — POST anyway
  // (the plan's unconditional send; the backend's per-session lock answers
  // 409, surfaced by handleSendError). No registration and no streaming slot:
  // the open turn's sink owns this session's frames. A POST that slips in
  // after the open turn's terminal (µs window) renders on the next reload —
  // noted, not solved here.
  if (harnessTurns.has(sessionId)) {
    await apiClient.post(endpoint, opts.body, { signal: opts.signal });
    return;
  }
  let resolveEnd!: () => void;
  const turnEnded = new Promise<void>(r => { resolveEnd = r; });
  const turn: _HarnessTurn = {
    sink: createTurnSink(get, set, {
      sessionId,
      userContent: opts.userContent,
      userParentId: opts.userParentId,
      optimisticUserId: opts.optimisticUserId,
      userImages: opts.userImages,
    }),
    settled: false,
    settle: () => resolveEnd(),
  };
  // Seat the assembler BEFORE the POST: the preamble (ids/sources) is emitted
  // server-side BEFORE the followup is even called, so WS frames can beat the
  // HTTP response — the registration and the boundary must both stand first.
  // The streaming slot itself was claimed by runCompletion before this call,
  // so the ids handler finds the controller waiting.
  beginTurn(sessionId, set);
  harnessTurns.set(sessionId, turn);
  try {
    await apiClient.post(endpoint, opts.body, { signal: opts.signal });
  } catch (e) {
    if (turn.settled) return;
    // No transport ever opened for this attempt (an unconfigured line emits
    // no frames at all — the HTTP status is the signal): un-register, close
    // the assembler window, and let runCompletion's catch surface it.
    harnessTurns.delete(sessionId);
    endTurn(sessionId, get().streaming?.messageId ?? null, set);
    throw e;
  }
  await turnEnded;
}

/** Start a /completions turn, mutating store state via set/get.
 * The POST returns while the turn runs; the frames (and the turn's end)
 * arrive as chat_frame envelopes through the registration above. */
export async function streamCompletion(
  get: () => ChatState,
  set: Set,
  opts: {
    sessionId: string;
    body: Record<string, unknown>;
    signal: AbortSignal;
    userParentId: string | null;
    userContent: string;
    userImages?: string[];
    optimisticUserId?: string;
  },
): Promise<void> {
  await _harnessCompletion(get, set, opts);
}

// Chat reset (project switch / logout): settle every pending harness turn so
// no runCompletion stays suspended on a turn whose frames will never come,
// then drop the registrations (late frames find no sink — see dispatch).
registerChatResetHandler(() => {
  for (const turn of harnessTurns.values()) {
    turn.settled = true;
    turn.settle();
  }
  harnessTurns.clear();
});
