/**
 * The browser feed — dsh's conversation assembler driven from Lore's chat
 * store.
 *
 * // SYSTEM: dsh-conversation — the BROWSER half of the ONE engine. The live
 * //   frame dispatch feeds
 * //   every verbatim dsh frame to `append` (the tail) and every `lore/*` mint
 * //   to `splice` (its seq may sit behind the tail — the splice INVARIANT in
 * //   the bundle routes by seq); a `dsh_stream` publication feeds the
 * //   transient arm (each chunk → an `assistant/live-chunk` entry, see
 * //   feedStreamFrame); a reload feeds the rows' replayed frames to
 * //   `replaceWindow`. Components read the PUBLISHED node data and never
 * //   recompute it — no second fold, no re-derived chip.
 *
 * // ARCH: the engine instances live HERE, module-level, not in Zustand —
 * //   they are opaque machines, not serializable state. The store carries the
 * //   PUBLICATION (the ordered node list as plain data) plus the turn→row
 * //   placement the renderer resolves against:
 * //   `conversation` (ChatConversationViewNode[] projected to plain objects),
 * //   `turnRanges` (assistant message id → the [min, max] seq window of that
 * //   turn's frames) and `turnStartSeq` (the live turn's boundary: nodes above
 * //   it render in the streaming message). Ownership test is inclusive on both
 * //   ends: turn windows are disjoint (the replay's per-turn frames carry the
 * //   turn's mint at anchor+offset INSIDE the window, and the next turn's
 * //   first seq is a whole integer above the previous turn/end), so a node
 * //   resolves to exactly one row. Session-level nodes between turns resolve
 * //   to no row and render nowhere — measured harmless (the plan's replay
 * //   note: only non-rendering kinds sit there).
 *
 * This module imports NOTHING from the store — slices hand in a `set`
 * callback (same dependency direction as reset-registry.ts).
 */
import {
  createLoreConversation,
  expandAssistantStream,
  type SessionEventLikeEntry,
  type ConversationPublication,
  type LoreConversation,
  type ProcessGroupInfo,
} from '../../dsh/lore-conversation.js';
import { registerChatResetHandler } from './reset-registry';

/**
 * One published node, plain-data form of ChatConversationViewNode. `data` is
 * the assembler's ChatNodeDataMap payload — read, never recomputed.
 */
export interface ConversationVM {
  key: string;
  kind: string;
  anchorSeq: number;
  data: unknown;
  /** dsh's process group holding this node (an assistant step: its reasoning
   * part) — read from the engine, never recomputed. Absent outside a group. */
  groupKey?: string;
  group?: ProcessGroupInfo;
}

/** One assistant row's turn window: every node anchored inside renders there. */
export interface TurnRange {
  min: number;
  max: number;
}

/** The publication patch the feed writes through the caller's `set`. Each
 * field is written INDEPENDENTLY — a boundary publishes its placement patch,
 * a publication publishes the node list (partial patches, like Zustand's). */
export interface FeedPublication {
  conversation?: ConversationVM[];
  turnRanges?: Record<string, TurnRange>;
  turnStartSeq?: number | null;
}

export type FeedSet = (patch: FeedPublication) => void;

interface FeedState {
  sessionId: string | null;
  engine: LoreConversation | null;
  /** Every relay frame fed this session — the lineage rewind's input. */
  fedFrames: Record<string, unknown>[];
  /** Per-assistant-row turn windows, rebuilt wholesale by replaceWindow. */
  turnRanges: Record<string, TurnRange>;
  /** The live turn's boundary (engine tail when the turn was seated). */
  turnStartSeq: number | null;
  /** Max seq fed — the engine tail from OUTSIDE the bundle. Durable frames
   * only: a transient chunk never advances it (its fraction sits below the
   * next durable seq by construction). */
  tailSeq: number;
  /** First seq fed after beginTurn — the live range's min. */
  turnMinSeq: number | null;
  /** The open dsh_stream attempt (start frame's turn/step — a chunk frame
   * carries neither; without a start the chunk lands nowhere honest). */
  streamAttempt: { attemptId: unknown; turn: number; step: number } | null;
  /** Chunks fed since the last durable frame — the fractional order within
   * the gap above the tail (dsh's own ClientAssistantStream counter). */
  transientInGap: number;
  /** Pending animation-frame publication (rAF handle or timeout id). */
  publishScheduled: ReturnType<typeof setTimeout> | null;
}

const feed: FeedState = {
  sessionId: null,
  engine: null,
  fedFrames: [],
  turnRanges: {},
  turnStartSeq: null,
  tailSeq: Number.NEGATIVE_INFINITY,
  turnMinSeq: null,
  streamAttempt: null,
  transientInGap: 0,
  publishScheduled: null,
};

function publish(cadence: ConversationPublication, set: FeedSet): void {
  if (cadence === 'none') return;
  if (cadence === 'animation-frame') {
    if (feed.publishScheduled !== null) return;
    // One coalesced publication per frame: rAF in the browser, a timeout where
    // no frame loop exists (jsdom). Why batch: streaming chunks publish at
    // token rate, and per-token store writes are the regression the old
    // per-chunk delta batching existed to prevent.
    const schedule = typeof requestAnimationFrame === 'function'
      ? (fn: () => void) => requestAnimationFrame(fn)
      : (fn: () => void) => setTimeout(fn, 16);
    feed.publishScheduled = schedule(() => {
      feed.publishScheduled = null;
      publishNow(set);
    }) as unknown as ReturnType<typeof setTimeout>;
    return;
  }
  publishNow(set);
}

/**
 * The published VM for a node object, memoized on that object.
 *
 * INVARIANT: an UNCHANGED node publishes the SAME `ConversationVM` object.
 * Why: the assembler rebuilds only the contexts an input dirtied, so an
 * untouched node keeps its identity across publications — and the renderer's
 * per-row slice reuse (MessageList) is an elementwise identity check against
 * exactly this. Minting a fresh VM per publication would make every row's
 * slice a new array on every streamed token; comparing anything COARSER than
 * identity (a length, a key) would freeze the streaming row instead, because a
 * growing assistant step keeps both its length and its key while its `data`
 * changes. A cached VM is reused only while its group is the same too — dsh
 * can regroup (or close a group) without touching the node itself.
 */
const vmCache = new WeakMap<object, ConversationVM>();

function publishNow(set: FeedSet): void {
  if (!feed.engine) return;
  const conversation: ConversationVM[] = [];
  const nodes = feed.engine.nodes();
  const groups = feed.engine.groups();
  for (const node of nodes) {
    if (node.visibility === 'hidden') continue;
    const groupKey = groups.byNode.get(node.key)?.groupKey;
    const group = groupKey === undefined ? undefined : groups.data.get(groupKey);
    const cached = vmCache.get(node);
    if (cached && cached.groupKey === groupKey && cached.group === group) {
      conversation.push(cached);
      continue;
    }
    const vm: ConversationVM = {
      key: node.key,
      kind: node.kind,
      anchorSeq: node.anchorSeq,
      // Every kind publishes — what renders is the renderer's decision (an
      // unrendered kind is the fallback chip's business, never a filter's).
      data: node.data,
      ...(groupKey !== undefined && group !== undefined ? { groupKey, group } : {}),
    };
    vmCache.set(node, vm);
    conversation.push(vm);
  }
  set({ conversation, turnRanges: feed.turnRanges, turnStartSeq: feed.turnStartSeq });
}

/** Reconstruct the SessionEvent the relay frame transported (map.ts
 * `toRelayFrame` is the event's transport, not a view of it) and wrap it as
 * the assembler's input. `time` defaults to 0 — a pure replay stays pure (no
 * wall clock); the bubble never shows these times. */
function toInput(frame: Record<string, unknown>, type: string): SessionEventLikeEntry {
  const event: SessionEventLikeEntry['event'] = {
    seq: frame.seq as number,
    type,
    data: frame.data ?? null,
    time: typeof frame.time === 'number' ? frame.time : 0,
    ...(frame.surfaceOp !== undefined ? { surfaceOp: frame.surfaceOp } : {}),
    ...(Array.isArray(frame.sourceEventSeqs) ? { sourceEventSeqs: frame.sourceEventSeqs } : {}),
    ...(frame.ignorable === true ? { ignorable: true } : {}),
  };
  return { type: 'event', event };
}

/** Whether this chat frame belongs to the assembler (a verbatim dsh event, a
 * lore mint, or a transient dsh_stream publication). Everything else — ids,
 * sources, done, unknown forward-compat types — is the live arms' business
 * and never reaches the engine. */
export function isFeedFrame(frame: { type?: unknown }): boolean {
  if (typeof frame.type !== 'string') return false;
  return frame.type === 'dsh_event' || frame.type === 'dsh_stream'
    || frame.type.startsWith('lore/');
}

/** Ensure the engine matches the session. A session switch resets the machine:
 * the next load feeds a fresh replaceWindow, so no stale node can survive. */
function ensureEngine(sessionId: string | null): LoreConversation {
  if (feed.sessionId !== sessionId || !feed.engine) {
    feed.sessionId = sessionId;
    feed.engine = createLoreConversation();
    feed.fedFrames = [];
    feed.turnRanges = {};
    feed.turnStartSeq = null;
    feed.tailSeq = Number.NEGATIVE_INFINITY;
    feed.turnMinSeq = null;
    feed.streamAttempt = null;
    feed.transientInGap = 0;
  }
  return feed.engine;
}

/**
 * Feed ONE live chat frame. `dsh_event` → `append` (the tail); `lore/*` →
 * `splice` (its seq may sit behind the tail — the detached image outcome);
 * `dsh_stream` → the transient arm below. A frame without a finite seq
 * cannot enter the assembler's seq space and is dropped (the relay emits
 * `seq: null` only for degenerate events).
 *
 * INVARIANT: the browser never mints a lore node — every lore/* frame it
 * feeds was minted by the backend and arrives verbatim. Why: a second mint
 * site in the browser drifted from the backend's three times; one producer
 * cannot disagree with itself. (The detached image-gen card arrives here the
 * same way: the backend mints it from the run's frozen anchor and ships it on
 * the chat channel; conversation-feed.test.ts pins that this module splices
 * EXACTLY once, here.)
 */
export function feedFrame(
  frame: Record<string, unknown>,
  sessionId: string | null,
  set: FeedSet,
): void {
  if (!isFeedFrame(frame)) return;
  const engine = ensureEngine(sessionId);
  if (frame.type === 'dsh_stream') {
    feedStreamFrame(frame, set);
    return;
  }
  const seq = frame.seq;
  if (typeof seq !== 'number' || !Number.isFinite(seq)) return;
  const type = frame.type as string;
  const input = toInput(frame, type === 'dsh_event' ? String(frame.kind ?? 'dsh') : type);
  feed.fedFrames.push(frame);
  feed.tailSeq = Math.max(feed.tailSeq, seq);
  feed.transientInGap = 0; // a durable frame closes the transient gap
  if (feed.turnStartSeq !== null && feed.turnMinSeq === null) feed.turnMinSeq = seq;
  const cadence = type === 'dsh_event' ? engine.append(input) : engine.splice(input);
  publish(cadence, set);
}

// ─── The transient live tail (dsh_stream) ────────────────────────────────────
//
// The v4 log holds only settled events: the live tail is dsh's own
// `agent/assistant-stream` publication, relayed verbatim by the plugin
// (ws-events.ts relayAssistantStream). The START frame opens the attempt (a
// chunk carries no turn/step of its own); each CHUNK becomes the assembler's
// transient `assistant/live-chunk` entry, seq'd fractionally above the
// durable tail — dsh's own ClientAssistantStream formula (cursor + 1 -
// 1/(k+1)), generalized for a fractional tail (a lore mint can sit mid-gap)
// and confined to the BAND above every mint offset: the k-th chunk lands at
// tail + gap·(BAND + (1−BAND)·k/(k+1)), strictly above the tail and strictly
// below the next integer — so the settlement (the next integer seq, fed as a
// normal dsh_event) supersedes the transient rows by APPENDING above them.
// WHY the band: the assembler's `append` answers 'none' to a seq it already
// holds, so a chunk seq equal to a mint seq drops whichever arrives second —
// the verdict/compaction card or a text delta — and the bare k/(k+1) series
// hits 0.5 (verdictAsk) at k=1 and 0.8 (compactionMint) at k=4 on an
// integral tail; the band starts above the largest offset. A durable frame resets the gap counter (feedFrame above). END retires the
// attempt's transient rows through the bundle's `settleAssistant` on EVERY
// outcome (feedStreamFrame below): an abandoned attempt's ghost text has no
// other remover, and a committed settlement arrives separately as a normal
// dsh_event. Chunks with no open attempt — a subscribe that lost the
// attempt's start — are dropped, dsh's own fold's rule; the settlement
// renders the text.

// The largest LORE_SEQ_OFFSETS value (compactionMint 0.8) as a pinned literal
// — the bundle exports no offsets (the same convention the backend's
// `_lore_event` mirrors); the collision test pins it against the mint offsets.
const STREAM_CHUNK_BAND = 0.9;

/** Append one transient live-chunk entry for the OPEN attempt at the next
 * band position (the WHY-the-band block above). Shared by the live `dsh_stream`
 * chunk arm and the reload baseline's replay — one series, one rule. */
function appendLiveChunk(
  attempt: { attemptId: unknown; turn: number; step: number },
  chunk: unknown,
  time: number,
  set: FeedSet,
): void {
  if (!feed.engine || !Number.isFinite(feed.tailSeq)) return;
  feed.transientInGap += 1;
  const gap = Math.floor(feed.tailSeq) + 1 - feed.tailSeq; // (0,1]; 1 on an integral tail
  const cadence = feed.engine.append({
    type: 'transient',
    event: {
      type: 'assistant/live-chunk',
      // No chunk seq equals a mint seq (see WHY the band above).
      seq: feed.tailSeq + gap * (STREAM_CHUNK_BAND
        + (1 - STREAM_CHUNK_BAND) * (feed.transientInGap / (feed.transientInGap + 1))),
      time,
      data: {
        attemptId: attempt.attemptId,
        turn: attempt.turn,
        step: attempt.step,
        chunk,
      },
    },
  });
  publish(cadence, set);
}

/** One `assistant_stream` baseline's ACTIVE attempt off the wire (the
 * plugin's fold, dsh's SessionAssistantStreamAccumulator snapshot) — the
 * reload's streamed text. */
interface StreamBaseline {
  attemptId: unknown;
  turn: number;
  step: number;
  nextIndex: number;
  stream: readonly unknown[];
}

/**
 * Seat the reload's streamed-text baseline (the open row's `assistant_stream`):
 * seat the feed's open attempt from the baseline's ACTIVE attempt and replay
 * the compact records as live-chunk entries through the SAME band path a live
 * chunk takes — the text is on screen before any live frame, and the next
 * live chunk continues at nextIndex's band position (the fold's expansion
 * length IS nextIndex by construction). A baseline with no active attempt, or
 * a malformed record set, skips the seat: the streamed text returns with the
 * settlement (today's behaviour), never garbled.
 */
export function adoptStreamBaseline(
  baseline: unknown,
  sessionId: string | null,
  set: FeedSet,
): void {
  const b = (baseline as { activeAttempt?: StreamBaseline } | null | undefined)
    ?.activeAttempt;
  if (!b || typeof b.turn !== 'number' || typeof b.step !== 'number'
    || !Array.isArray(b.stream)) return;
  ensureEngine(sessionId);
  let timed;
  try {
    timed = expandAssistantStream(b.stream as never);
  } catch (e) {
    console.warn('assistant_stream baseline unreadable — streamed text returns with settlement', e);
    return;
  }
  feed.streamAttempt = { attemptId: b.attemptId, turn: b.turn, step: b.step };
  const attempt = feed.streamAttempt;
  for (const member of timed) {
    appendLiveChunk(attempt, member.chunk, member.time, set);
  }
}

function feedStreamFrame(frame: Record<string, unknown>, set: FeedSet): void {
  const stream = frame.frame as Record<string, unknown> | undefined;
  if (!stream || typeof stream.type !== 'string') return;
  if (stream.type === 'start') {
    feed.streamAttempt = typeof stream.turn === 'number' && typeof stream.step === 'number'
      ? { attemptId: stream.attemptId, turn: stream.turn, step: stream.step }
      : null;
    return;
  }
  if (stream.type === 'end') {
    // The attempt's terminal: retire its transient rows NOW, on every
    // outcome — the end names its OWN attempt (not the open one), so a lost
    // start's end is a no-op retirement and never clobbers a different open
    // attempt. The open attempt closes only when the end names it.
    const cadence = feed.engine
      ? feed.engine.settleAssistant(stream.attemptId)
      : 'none';
    if (feed.streamAttempt !== null && feed.streamAttempt.attemptId === stream.attemptId) {
      feed.streamAttempt = null;
    }
    publish(cadence, set);
    return;
  }
  if (stream.type !== 'chunk') return;
  const attempt = feed.streamAttempt;
  if (!attempt || attempt.attemptId !== stream.attemptId) return;
  appendLiveChunk(
    attempt, stream.chunk,
    typeof stream.time === 'number' ? stream.time : 0, set,
  );
}

/**
 * Seat the live turn: record the engine tail as the streaming message's
 * boundary (the `ids` handler calls this). Nodes anchored above it render in
 * the streaming message; everything up to it belongs to earlier rows.
 *
 * `boundary`: an ADOPTED turn
 * (reload mid-turn) seats at the OPEN window's min seq instead of the tail —
 * the whole open turn renders in the streaming slot, and
 * endTurn's merge then spans replay + live frames.
 */
export function beginTurn(
  sessionId: string | null, set: FeedSet, boundary?: number,
): void {
  ensureEngine(sessionId);
  feed.turnStartSeq = typeof boundary === 'number' && Number.isFinite(boundary)
    ? boundary
    : feed.tailSeq;
  feed.turnMinSeq = null;
  set({ turnStartSeq: feed.turnStartSeq });
}

/**
 * Close the live turn: bind the nodes fed since beginTurn to the assistant
 * message id (the flush path calls this), so the settled row keeps rendering
 * from the assembler without a reload.
 */
export function endTurn(sessionId: string | null, messageId: string | null, set: FeedSet): void {
  ensureEngine(sessionId);
  if (messageId && feed.turnStartSeq !== null) {
    // MERGE with any window already bound to this row: a resumed turn (the
    // approve-resume chain) streams a second window onto the SAME assistant
    // message, and overwriting would strand the first stream's nodes.
    const existing = feed.turnRanges[messageId];
    feed.turnRanges = {
      ...feed.turnRanges,
      // The window's min is the FIRST frame fed during the stream (inclusive
      // ownership, same rule a reload's ranges follow) — the turnStartSeq
      // fallback covers a stream that fed nothing.
      [messageId]: {
        min: Math.min(existing?.min ?? Number.POSITIVE_INFINITY, feed.turnMinSeq ?? feed.turnStartSeq),
        max: Math.max(existing?.max ?? Number.NEGATIVE_INFINITY, feed.tailSeq),
      },
    };
  }
  feed.turnStartSeq = null;
  feed.turnMinSeq = null;
  set({ turnRanges: feed.turnRanges, turnStartSeq: null });
}

/**
 * Rewind the assembler to the lineage the NEXT turn extends (runCompletion
 * calls this BEFORE the boundary is seated). A fork sends on an ancestor
 * chain while the engine still holds the PREVIOUS lineage's window: the fork
 * turn runs in a fresh dsh session whose seqs overlap the abandoned rows'
 * (a root fork restarts at 0, a real fork reuses the space above its seeded
 * prefix), so `append` drops them as already-held seqs and the streaming row
 * stays empty until a reload rebuilds the window — the fork repro.
 *
 * Keeps only the fed frames whose seq falls inside the given chain's turn
 * ranges (inclusive ownership, the renderer's own placement rule), re-windows
 * the engine with them, and recomputes tailSeq / turnRanges. A rewound-away
 * row renders from its own fields, without chips, until it is the active
 * lineage again — the same rule the reload path follows for abandoned rows.
 *
 * No-op (no replaceWindow, no publish) when every bound range is already in
 * the chain: a linear send pays nothing.
 */
export function rewindToLineage(
  messageIds: ReadonlyArray<string>,
  sessionId: string | null,
  set: FeedSet,
): void {
  const engine = ensureEngine(sessionId);
  const keep = new Set(messageIds);
  if (Object.keys(feed.turnRanges).every(id => keep.has(id))) return;
  const ranges: Record<string, TurnRange> = {};
  for (const id of messageIds) {
    const range = feed.turnRanges[id];
    if (range) ranges[id] = range;
  }
  const keptRanges = Object.values(ranges);
  const frames: Record<string, unknown>[] = [];
  const entries: SessionEventLikeEntry[] = [];
  let tail = Number.NEGATIVE_INFINITY;
  for (const frame of feed.fedFrames) {
    const seq = frame.seq;
    if (typeof seq !== 'number' || !Number.isFinite(seq)) continue;
    const type = typeof frame.type === 'string' ? frame.type : '';
    if (!type) continue;
    if (!keptRanges.some(r => seq >= r.min && seq <= r.max)) continue;
    frames.push(frame);
    tail = Math.max(tail, seq);
    entries.push(toInput(frame, type === 'dsh_event' ? String(frame.kind ?? 'dsh') : type));
  }
  feed.fedFrames = frames;
  feed.turnRanges = ranges;
  feed.turnStartSeq = null;
  feed.tailSeq = tail;
  feed.turnMinSeq = null;
  feed.streamAttempt = null;
  feed.transientInGap = 0;
  engine.replaceWindow(entries, false);
  publishNow(set);
}

/**
 * Replace the whole window from a reload's rows (GET /messages / the sessions
 * piggyback). Each assistant row's `frames` is that turn's replayed relay
 * frames with the backend's lore mints already inserted; concatenated in row
 * order they ARE the session window. Rows without frames contribute nothing —
 * the assembler renders what the log holds, and the row's own fields cover the
 * rest (the timeline INVARIANT on the read path).
 */
export function replaceWindowFromRows(
  rows: ReadonlyArray<object>,
  sessionId: string | null,
  set: FeedSet,
): void {
  const engine = ensureEngine(sessionId);
  const entries: SessionEventLikeEntry[] = [];
  feed.fedFrames = [];
  feed.turnRanges = {};
  feed.turnStartSeq = null;
  feed.tailSeq = Number.NEGATIVE_INFINITY;
  feed.turnMinSeq = null;
  feed.streamAttempt = null;
  feed.transientInGap = 0;
  for (const rowObj of rows) {
    const row = rowObj as Record<string, unknown>;
    const frames = row.frames;
    if (!Array.isArray(frames) || frames.length === 0) continue;
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;
    for (const raw of frames) {
      if (typeof raw !== 'object' || raw === null) continue;
      const frame = raw as Record<string, unknown>;
      const seq = frame.seq;
      if (typeof seq !== 'number' || !Number.isFinite(seq)) continue;
      const type = typeof frame.type === 'string' ? frame.type : '';
      if (!type) continue;
      feed.fedFrames.push(frame);
      feed.tailSeq = Math.max(feed.tailSeq, seq);
      min = Math.min(min, seq);
      max = Math.max(max, seq);
      const kind = type === 'dsh_event' ? String(frame.kind ?? 'dsh') : type;
      entries.push(toInput(frame, kind));
    }
    const mid = typeof row.message_id === 'string' ? row.message_id : '';
    if (mid && min !== Number.POSITIVE_INFINITY) {
      feed.turnRanges[mid] = { min, max };
    }
  }
  engine.replaceWindow(entries, false);
  publishNow(set);
}

/**
 * Drop the row's wire-only keys before it enters the store.
 *
 * INVARIANT: no row carries `frames` into Zustand. Why: the frames are the
 * ASSEMBLER's input, consumed once by replaceWindowFromRows — keeping them on
 * the row would hold a second copy of every turn's whole driver replay in the
 * store, and invite a component to fold them a second time. What renders is
 * the published nodes; a frameless row still renders from its own fields.
 * `open_turn` and its `assistant_stream` join it: adoptOpenTurn consumes both
 * beside the fold, and the store never carries them.
 */
export function stripFrames<T extends {
  frames?: unknown; open_turn?: unknown; assistant_stream?: unknown;
}>(row: T): T {
  if (row.frames === undefined && row.open_turn === undefined
    && row.assistant_stream === undefined) return row;
  const {
    frames: _frames, open_turn: _openTurn, assistant_stream: _assistantStream, ...rest
  } = row;
  return rest as T;
}

/**
 * Republish the timeline as EMPTY — the STORE half only, no engine switch.
 * The ownership watcher in chat-store.ts calls this on every activeSessionId
 * transition; it never touches the module engine (exiting mid-stream must not
 * drop the old chat's engine, whose still-arriving frames keep appending —
 * the engine switches lazily at the next ensureEngine).
 */
export function publishTimelineReset(set: FeedSet): void {
  set({ conversation: [], turnRanges: {}, turnStartSeq: null });
}

/** Clear the feed's render state (session switch to a ghost / chat reset). */
export function clearFeed(set: FeedSet): void {
  ensureEngine(null);
  publishTimelineReset(set);
}

// The chat reset chokepoint (project switch / logout) clears the module state;
// the store patch itself lives in misc-slice.reset.
registerChatResetHandler(() => {
  feed.sessionId = null;
  feed.engine = null;
  feed.fedFrames = [];
  feed.turnRanges = {};
  feed.turnStartSeq = null;
  feed.tailSeq = Number.NEGATIVE_INFINITY;
  feed.turnMinSeq = null;
  feed.streamAttempt = null;
  feed.transientInGap = 0;
  if (feed.publishScheduled !== null) {
    clearTimeout(feed.publishScheduled);
    feed.publishScheduled = null;
  }
});

/** Test-only: schedule one pending publication immediately (a jsdom test has
 * no frame loop worth waiting for). */
export function __flushFeedPublishForTest(set: FeedSet): void {
  if (feed.publishScheduled !== null) {
    clearTimeout(feed.publishScheduled);
    feed.publishScheduled = null;
  }
  publishNow(set);
}
