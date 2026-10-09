/** Chat turn transport + flushStreaming reducer.
 *
 * ONE transport: the turn is DRIVER-owned — POST /completions answers JSON
 * and the frames arrive as project-WS chat_frame envelopes, dispatched
 * through createTurnSink into the frame handlers + the assembler feed. */
import type { ChatMessage, ChatSource } from '../../types';
import { apiClient } from '../../api/client';
import { useAppStore } from '../app-store';
import { t } from '../../i18n';
import type { ChatState, Set, StreamingState, TurnEndReason } from './types';
import { validateFrame, type Frame } from './frame-validate';
import { registerChatResetHandler } from './reset-registry';
import {
  isFeedFrame, feedFrame, beginTurn, endTurn, adoptStreamBaseline,
} from './conversation-feed';
import { scheduleTurnEndFlush } from './queue-slice';

/** A wire field is usable as display text only when it is a non-empty string —
 * the unguarded terminal frames render a fallback otherwise (see `error` /
 * `lore/halt`). */
const isNonEmptyStr = (v: unknown): v is string =>
  typeof v === 'string' && v.length > 0;

/** A fresh, idle streaming-state object (no controller) for `sessionId`'s turn. */
export function emptyStreaming(sessionId: string): StreamingState {
  return { sessionId, messageId: null, content: '', controller: null };
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

/** The per-turn end facts, held on the harness REGISTRATION (not the slot):
 * a turn that ends while its chat is not shown has no slot to read them
 * from, and the queue rule (scheduleTurnEndFlush) hangs on the reason.
 * `reason` is stamped by the `error` / `lore/halt` arms; `aborted` by
 * stopGeneration before it aborts. */
export interface TurnEndFacts {
  reason?: TurnEndReason;
  aborted: boolean;
}

/** The per-turn facts the frame handlers close over — the turn's opts
 * and the harness registration's context are the same shape. */
export interface TurnFramesCtx {
  sessionId: string;
  userContent: string;
  userParentId: string | null;
  optimisticUserId?: string;
  userImages?: string[];
  /** The registration's end facts (see TurnEndFacts). */
  end: TurnEndFacts;
}

function createTurnSink(
  get: () => ChatState,
  set: Set,
  ctx: TurnFramesCtx,
): { frame: (ev: Frame) => void; accept: (obj: unknown) => Frame | null; warnMalformed: (e: unknown) => void } {
  const opts = ctx;
  let sourcesForMsg: ChatSource[] = [];
  let warnedMalformed = false;  // one-shot per stream (see the drop site below)

  // DISPLAY work (the assembler feed, the messages buffer, the slot) belongs
  // to the SHOWN chat only — read at frame time, so a turn that outlives its
  // chat being shown stops touching the display the instant the user leaves.
  // ROW work (session_title, context_usage) and end-fact stamping key on
  // opts.sessionId and run always.
  const isShown = (): boolean => get().activeSessionId === opts.sessionId;

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
      // DISPLAY work: the rows land in the shown chat's buffer only. A turn
      // whose chat is not shown keeps running without rows here — the reload
      // replays them on return.
      if (!isShown()) return;
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
      // ARCH: no sibling selection is written —
      // a branch's rows are a linear chain and the freshest-lineage walk
      // picks the new pair on its own (they are the newest rows).
      set({
        messages: newMessages,
        streaming: {
          // Preserve the controller created at send time; reset the per-message
          // accumulators for the (possibly resumed) turn.
          ...(get().streaming ?? emptyStreaming(opts.sessionId)),
          sessionId: opts.sessionId,
          messageId: ev.assistant_message_id,
        },
      });
    },
    error(ev) {
      console.error('Chat stream error:', ev.message);
      // WHY: a trailing `error` frame that arrives
      // while the user has already pressed Stop is part of the deliberate
      // stop, not a failure — skip the end stamp, inline error + toast.
      // The stamp lives on the registration (stopGeneration set it before
      // aborting), so the check works even after the slot is long gone.
      // Mirrors the AbortError guard in messages-slice.handleSendError (no failure
      // badge / toast on a user-initiated stop). Why: the backend can emit an error
      // frame as the aborted turn winds down, and surfacing it would falsely report a
      // failure for an action the user chose.
      if (ctx.end.aborted) return;
      // The turn is ending in failure — the queue restores instead of auto-firing.
      ctx.end.reason = 'error';
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
      // This is a BACKEND-minted frame, not a dsh event, so the assembler
      // builds no node for it — and an assembled turn renders its nodes
      // INSTEAD of `content`, which would leave the appended notice unread on
      // exactly the turns that have a timeline. It also covers a frame arriving
      // BEFORE `ids`, which has no row to write into and does NOT throw: the
      // stream completes normally, so runCompletion's catch never fires.
      // WHY: an `error` frame always reaches the user as a toast — it is the
      // one signal that does not depend on which branch the bubble took.
      useAppStore.getState().showToast(t('chatSendFailed'), 'error');
      // The content append is DISPLAY work — only the shown chat's row carries it.
      if (!isShown()) return;
      const mid = get().streaming?.messageId ?? null;
      set(s => ({
        messages: s.messages.map(m =>
          mid && m.message_id === mid ? { ...m, content: (m.content ?? '') + errMsg } : m,
        ),
      }));
    },
    sources(ev) {
      sourcesForMsg = mergeSources(sourcesForMsg, ev.sources as ChatSource[]);
      // Apply now if the assistant message already exists; otherwise the `ids`
      // handler seeds it from the accumulator (order-independent). The messages
      // write is DISPLAY work — gated like every write into the shown buffer.
      if (!isShown()) return;
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
      if (!isShown()) return;
      const mid = get().streaming?.messageId ?? null;
      if (!mid || !isNonEmptyStr(ev.content)) return;
      const content = ev.content as string;
      set(s => ({
        messages: s.messages.map(m => (m.message_id === mid ? { ...m, content } : m)),
      }));
    },
    // The halt CARD is the assembler's node; the end REASON is stamped on the
    // registration's end facts so the turn's terminal does not auto-fire the
    // queue (see scheduleTurnEndFlush's INVARIANT on 'halted') — the stamp
    // survives even when the chat is not shown.
    'lore/halt'() {
      ctx.end.reason = 'halted';
    },
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
    // arm (lore/compaction-mint's toast). The engine renders the SHOWN
    // chat only — a frame for a chat that is not shown never reaches it
    // (the return path is the reload's replay), so it cannot reset the
    // engine out from under the displayed timeline.
    if (isFeedFrame(event) && isShown()) feedFrame(event as Record<string, unknown>, opts.sessionId, set);
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
// for a harness session resolves only when the turn ENDS: the terminal frame
// (`turn_closed` — the ONLY terminal; the plugin pushes it after the turn's
// last mapped frame, the replay carries it for a session with no registered
// turn, the channel mints it on a deadline breach, the setup-failure tail
// carries it after `error`) settles it, so
// runCompletion's catch/finally see the turn's lifecycle end (the
// transport terminal), not the POST's return. `done` is a CONTENT frame — the
// backend's text fold for the row, minted before finalize + lock release;
// treating it as a terminal made the queue flush POST into the held turn
// lock.
//
// The registration is per CHAT session id (the WS envelope's session_id — the
// fan-out keys it by the chat id even for continuation chats). Frames for a
// session with NO registration are ignored: another tab's turn, or a turn this
// tab never sent. A reload mid-turn is NOT that case — adoptOpenTurn seats the
// open turn's assistant_stream baseline and registers the sink, so the next
// live chunk continues the band.

interface _HarnessTurn {
  sink: ReturnType<typeof createTurnSink>;
  settled: boolean;
  settle: () => void;
  /** The turn's end facts (see TurnEndFacts) — shared by reference with the
   * sink that stamps them and stopGeneration that aborts them. */
  end: TurnEndFacts;
  /** The assistant row the turn writes — from its `ids` frame (or the open
   * row it was adopted on), recorded whether or not the chat is shown, so a
   * return seats the slot before the reload lands. */
  messageId: string | null;
}

const harnessTurns = new Map<string, _HarnessTurn>();

/** Whether THIS tab holds an open harness turn registration for the session —
 * the browser-WS-gap resync's trigger (a reconnect reloads the session when
 * this is true: frames lost in the socket gap recover through the reload's
 * replay + re-adoption), and the sendMessage queue guard's second clause
 * (the return window: the slot is null, the registration still open). */
export function hasOpenHarnessTurn(sessionId: string): boolean {
  return harnessTurns.has(sessionId);
}

/** The slot for a chat being opened: seated at once from its open
 * registration (Stop and the busy composer show before the reload lands —
 * a send in that window queues, see sendMessage's guard), null when the chat
 * has no open turn in this tab. The reload's adoption re-seats it on the
 * open row. */
export function registeredTurnSlot(sessionId: string): StreamingState | null {
  const turn = harnessTurns.get(sessionId);
  if (!turn) return null;
  return { ...emptyStreaming(sessionId), controller: new AbortController(), messageId: turn.messageId };
}

/** Stop's stamp: the ACTIVE chat's registration is marked aborted BEFORE the
 * controller aborts — the end facts live on the registration (a turn that
 * ends while its chat is not shown has no slot to read), and a trailing
 * `error` frame of the deliberate stop must not surface as a failure. */
export function markHarnessTurnAborted(sessionId: string): void {
  const turn = harnessTurns.get(sessionId);
  if (turn) turn.end.aborted = true;
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
  rows: ReadonlyArray<{
    message_id?: string; open_turn?: boolean; assistant_stream?: unknown;
  }>,
): void {
  const reg = harnessTurns.get(sessionId);
  const open = rows.find(r => r.open_turn === true);
  if (!open || typeof open.message_id !== 'string' || !open.message_id) {
    if (reg && !reg.settled) {
      // The turn ended inside our gap: close what the lost terminal cannot.
      // (No registration + no open row: nothing to do — plain reload.)
      if (get().activeSessionId === sessionId) {
        endTurn(sessionId, get().streaming?.messageId ?? null, set);
        set(flushStreaming);
      }
      harnessTurns.delete(sessionId);
      reg.settled = true;
      reg.settle();
      // How it ended died with the socket — never auto-fire the queue on a guess.
      scheduleTurnEndFlush(get, sessionId, 'lost');
    }
    return;
  }
  // Seat the slot with a controller: Stop on an adopted turn routes through
  // stopGeneration's cancel POST exactly like a live one.
  set({ streaming: { ...emptyStreaming(sessionId), controller: new AbortController(), messageId: open.message_id } });
  // The boundary is the OPEN window's min (replaceWindowFromRows just
  // published it) — the whole open turn renders in the streaming slot.
  beginTurn(sessionId, set, get().turnRanges[open.message_id]?.min);
  // The reload's streamed text: the open row carries the plugin's fold of the
  // live stream (`assistant_stream`) — seat the attempt and replay its
  // compact records so the already-streamed text is on screen immediately;
  // the next live chunk continues the same band series.
  if (open.assistant_stream !== undefined) {
    adoptStreamBaseline(open.assistant_stream, sessionId, set);
  }
  if (!reg) {
    let resolveEnd!: () => void;
    const turnEnded = new Promise<void>(r => { resolveEnd = r; });
    const end: TurnEndFacts = { aborted: false };
    harnessTurns.set(sessionId, {
      sink: createTurnSink(get, set, {
        sessionId, userContent: '', userParentId: null, end,
      }),
      settled: false,
      settle: () => resolveEnd(),
      end,
      messageId: open.message_id,
    });
    // Nobody awaits an adopted turn's promise — but the registration MUST
    // carry one so the terminal path (dispatchChatFrame) stays uniform.
    void turnEnded;
  }
}

/** The frame type that ENDS a turn on the WS transport: `turn_closed` —
 * pushed by the plugin after the turn's last mapped frame (graceful and
 * errored ends), carried by the replay for a session with no registered
 * driver-owned turn (a resync gap re-delivers it — a duplicate after a
 * delivered live push reaches a registration that no longer exists and
 * drops, see streaming.turn-terminal.test.ts), minted by the channel on a
 * deadline breach, and emitted as the setup-failure tail (error +
 * turn_closed).
 * `done` is NOT in the set: it is a content frame — the backend's text fold
 * for the row.
 * INVARIANT: on a GRACEFUL end `turn_closed` is the frame that follows the
 * backend's turn-lock release. Why: the queue flush (which fires only on a
 * clean end) POSTs into a free lock and no later terminal of the same turn
 * can reach the follow-up's registration — whereas `done` is emitted BEFORE
 * finalize + lock release, so flushing on it hits the held lock (409; the
 * chips already cleared, so the text is lost) or has the follow-up's fresh
 * registration closed by the previous turn's late `turn_closed` (the
 * follow-up renders nothing). NOT claimed: that `turn_closed` always
 * arrives after the release — on the setup-failure and refused paths the
 * frames are emitted before `_teardown_turn_lock`, and the plugin pushes
 * the refusal's `turn_closed` independently of the backend teardown;
 * harmless there, because an error end restores the queue instead of
 * flushing it. */
const TERMINAL_FRAME_TYPES: ReadonlySet<string> = new Set(['turn_closed']);

/** The end reason of the registered turn: a stamped error/halt wins, a Stop
 * shows as the aborted stamp, anything else is a clean end. Read off the
 * REGISTRATION — a turn that ends while its chat is not shown has no slot. */
function turnEndReason(turn: _HarnessTurn): TurnEndReason {
  if (turn.end.reason) return turn.end.reason;
  return turn.end.aborted ? 'aborted' : 'done';
}

/** One project-WS chat_frame envelope, dispatched into the open harness turn.
 * Project-connection stays transport-dumb: this is the store-side sink. */
export function dispatchChatFrame(
  get: () => ChatState,
  set: Set,
  sessionId: string,
  frame: unknown,
): void {
  const turn = harnessTurns.get(sessionId);
  if (!turn) {
    // No registration: another tab's turn, a reload that adopted nothing —
    // or a DETACHED RUN. Its facts ride the chat channel as lore/image-gen
    // frames with no open turn; one for the ACTIVE session feeds the
    // assembler (the card renders live), everything else keeps dropping (a
    // non-active session shows its card on return through the reload).
    // WHY lore/image-gen only: the other lore kinds (verdict-ask, halt,
    // compaction-mint) belong to a TURN — a tab already open on the chat has
    // no registration while another tab runs one (adoption is on
    // loadMessages), and feeding them here renders orphan cards without it.
    if (sessionId !== get().activeSessionId) return;
    if ((frame as { type?: unknown } | null)?.type !== 'lore/image-gen') return;
    const validated = validateFrame(frame);
    if (!validated) {
      console.warn('Chat frame handler failed: malformed lore frame', frame);
      return;
    }
    feedFrame(validated, sessionId, set);
    return;
  }
  const raw = frame as { type?: unknown; assistant_message_id?: unknown } | null;
  if (raw?.type === 'ids' && isNonEmptyStr(raw.assistant_message_id)) {
    turn.messageId = raw.assistant_message_id;
  }
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
    // Why the turn ended is read off the registration BEFORE anything else.
    const reason = turnEndReason(turn);
    // The turn's transport end. The DISPLAY half — binding the fed nodes to
    // the streaming row, flushing the slot — belongs to the SHOWN chat only:
    // endTurn's ensureEngine would reset the module engine off whichever chat
    // is displayed, and the slot of another chat is never this terminal's to
    // flush. A turn that ends while its chat is not shown settles through
    // here and re-seats from the reload on return.
    if (get().activeSessionId === sessionId) {
      endTurn(sessionId, get().streaming?.messageId ?? null, set);
      set(flushStreaming);
    }
    harnessTurns.delete(sessionId);
    turn.settled = true;
    turn.settle();
    scheduleTurnEndFlush(get, sessionId, reason);
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
  let resolveEnd!: () => void;
  const turnEnded = new Promise<void>(r => { resolveEnd = r; });
  const end: TurnEndFacts = { aborted: false };
  const turn: _HarnessTurn = {
    sink: createTurnSink(get, set, {
      sessionId,
      userContent: opts.userContent,
      userParentId: opts.userParentId,
      optimisticUserId: opts.optimisticUserId,
      userImages: opts.userImages,
      end,
    }),
    settled: false,
    settle: () => resolveEnd(),
    end,
    messageId: null,
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
    // no frames at all — the HTTP status is the signal): un-register, and let
    // runCompletion's catch surface it. The assembler window binds only for
    // the chat it renders — endTurn for a chat that is not shown would reset
    // the module engine off the displayed timeline.
    harnessTurns.delete(sessionId);
    if (get().activeSessionId === sessionId) {
      endTurn(sessionId, get().streaming?.messageId ?? null, set);
    }
    // A turn that never opened is no clean end: the queue goes back to the composer.
    scheduleTurnEndFlush(get, sessionId, opts.signal.aborted ? 'aborted' : 'error');
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
