/**
 * Pure replay projection: a session's dsh event log → per-turn frame lists.
 *
 * # SYSTEM: harness-driver (replay half) — the RELOAD input of the one
 *   projection. The frames this returns are the SAME frames the live
 *   channel delivers (map.ts's verbatim `dsh_event` relay + the
 *   `lore/verdict-ask` and `lore/halt` mints), grouped by turn, so a reload
 *   and the live stream agree by
 *   construction rather than by two synchronized copies of the timeline.
 *   One declared addition: for a session with no registered driver-owned
 *   turn, every closed turn's frames end with the transport terminal
 *   `turn_closed` (see projectSessionEntries's `terminals`) — the live
 *   channel pushes the same fact best-effort and unsequenced, and only the
 *   replay can recover its loss.
 *   PURE — no ctx, no http (test/entries.test.ts).
 *
 * # ARCH: the driving session's log is the ONLY input. A child (subagent)
 *   session keeps its own log, which the projection never reads; the driving
 *   turn's own dispatch replays. Why: inspect() takes one session id, and
 *   walking children would need the child ids the driving log does not record.
 *
 * # ARCH: the replay does not fold the log into a step timeline —
 *   it emits the events VERBATIM (toRelayFrame, the same builder map.ts uses)
 *   and the browser assembles the conversation from them. The mints are the
 *   two whose anchors sit inside dsh session events: `lore/verdict-ask` (on
 *   `approval/asked`) and `lore/halt` (on a `turn/end` whose reason dsh
 *   renders no node for). The other lore facts are minted Lore-side on
 *   reload: the backend's timeline attach mints `lore/image-gen` from the
 *   row's gen_steps, `lore/halt` from the row's halt column, and
 *   `lore/compaction-mint` from the compaction/end frame's mint outcome.
 */

import type { SessionAssistantStreamBaseline } from '@deepseek-ai/dsh-api-session-controller/types'

import { mapEvent, newTurnMapState, type DshEvent } from './map.ts'

/** One replayed turn: the frames the live stream sent, in log order, plus
 * the dsh log seq of its closing `turn/end` — the SAME id the backend stamps
 * on the turn's assistant row (messages.driver_seq), so the read path keys
 * rows to turns by stamp instead of position (a fork re-seeds the log to the
 * active lineage, breaking any positional pairing). `end_seq` ABSENT marks
 * an OPEN turn (no closing row — a mid-turn replay): the resync primitive;
 * which record a session renders for it, this open turn or the halt card, is
 * the consumer's split (the replay-split INVARIANT below). An open turn may
 * also carry `assistant_stream` — the live-stream fold's snapshot (see
 * projectSessionEntries), the reload's streamed-text baseline. */
export interface ReplayedTurn {
  frames: Record<string, unknown>[]
  end_seq?: number
  assistant_stream?: SessionAssistantStreamBaseline
}

/** The whole session-entries reply: the turns plus the log's tail seq — the
 * anchor a turn-less lore mint uses when no terminal frame exists (the
 * backend's reload `lore/halt` mint for an abnormally ended turn). */
export interface ReplayedSession {
  turns: ReplayedTurn[]
  tail_seq: number | null
}

/** The replayed transport terminal's fractional offset after the closing
 * turn/end's seq — the LORE_SEQ_OFFSETS PATTERN (a fractional seq between the
 * anchor event and the next one), not that table: turn_closed is TRANSPORT
 * state, not an assembler fact, so it never enters the lore registry. The
 * value sits above every lore offset (0.5/0.6/0.7/0.8) so the terminal always
 * orders after a mint anchored at the same turn/end — the halt mint included. */
const TURN_CLOSED_OFFSET = 0.9

/**
 * Group a session log into turns and map each turn's events to relay frames.
 *
 * A turn is `turn/start` … `turn/end`. Events OUTSIDE any turn — a fork seed's
 * leading rows, the session-level bookkeeping dsh writes between turns — buffer
 * into the next turn that opens, so nothing is dropped for sitting BETWEEN two
 * turns. Events sitting after the LAST `turn/end` get no turn to flush into and
 *   are NOT replayed, although the live channel relayed them. That is harmless
 * because of what actually lands there: over 283 real gray session logs, only 4
 * carry such a tail, and its every event is `agent/inbox/spliced` (2) or
 * `session/end-seed` (2) — neither renders. The inbox definition publishes
 * `'none'` (it only accumulates state for LATER nodes, and there are none after
 * the last turn), and `session/end-seed` has no conversation node definition at
 * all. A kind that DID render would change what a reader sees on reload, so a
 * new trailing kind is a decision, not a detail. A log whose last turn never
 * ended yields an OPEN ReplayedTurn — its frames, no `end_seq`. That is the
 * NORMAL reload case once the driver owns the turn lifecycle (a read happens
 * mid-turn), and the abnormal case (a driver crash, a deadline trip) is told
 * apart Lore-side, not here — see the replay-split INVARIANT at the emitting
 * branch.
 *
 * `sinceSeq` (optional, the resync boundary): the last frame seq a consumer
 * HOLDS — mints included, so the value may be fractional. Filtering happens
 * AFTER mapping, never before: the map state still evolves over the whole log
 * (it resets per turn at `turn/start`), so the surviving frames are the SAME
 * frames the live channel relayed above that seq — a resync is a subsequence
 * of the full replay, not a re-derivation. A closed turn left with no
 * surviving frames drops (the consumer already holds it whole, `turn/end`
 * included); the OPEN turn never drops — zero new frames still answers "the
 * turn is open". `tail_seq` stays the WHOLE log's tail regardless: it anchors
 * the reload halt mint and the log's high-water mark, neither of which is a
 * frame.
 *
 * `assistantStream` (optional): the live-stream fold's snapshot
 * (stream-baselines.ts, dsh's SessionAssistantStreamAccumulator). It rides
 * the OPEN turn only, and only while it holds an ACTIVE attempt that BEGAN
 * inside that turn — `startedAfterSeq` at or above the open turn's own
 * `turn/start` seq, recorded in the same projection pass (one id space, one
 * log — never a boundary seq from outside it): a turn that died without a
 * stream `end` (a driver crash) leaves its attempt active in the fold, and
 * serving its baseline onto a LATER open turn would seat dead text on a live
 * turn. A revision-only fold (nothing streaming, or a missed frame reset it)
 * seats nothing on a reload and is not carried. The resync keeps it beside
 * the surviving frames for the same reason the reload carries it: the
 * browser re-seats the transient tail from it.
 *
 * `terminals` (optional, default off): append the transport terminal
 * `turn_closed` (seq-anchored at each closed turn's `end_seq` +
 * TURN_CLOSED_OFFSET) after the turn's last frame. The caller gates it on
 * liveness — the session has NO registered driver-owned turn (the same
 * `followupTurns` key the crash closers branch on): a live session's running
 * turn is OPEN (no end_seq → no terminal) and its previous turns' terminals
 * are the LIVE push's to deliver, never the replay's. Why the replay carries
 * it at all: the plugin's live push is best-effort and NOT a log entry, so a
 * socket gap that swallows it would leave the browser's only terminal
 * undeliverable — with the terminal INSIDE the frames list, the since_seq
 * filter and the per-turn grouping handle it like any frame (a consumer that
 * holds through the terminal drops the turn whole; one that holds only
 * through the last mint receives the terminal alone).
 */
export function projectSessionEntries(
  events: DshEvent[], sinceSeq?: number,
  assistantStream?: SessionAssistantStreamBaseline,
  terminals?: boolean,
): ReplayedSession {
  const turns: ReplayedTurn[] = []
  let pending: DshEvent[] = []
  let open: {
    frames: Record<string, unknown>[]
    state: ReturnType<typeof newTurnMapState>
    /** The turn/start row's seq — the coordinate the fold's active attempt
     * must clear (startedAfterSeq at or above it) to seat on this open
     * turn. Undefined on a seq-less start row: unprovable, so nothing
     * seats. */
    startSeq: number | undefined
  } | null = null
  let tail: number | null = null

  for (const ev of events) {
    if (typeof ev.seq === 'number') tail = Math.max(tail ?? Number.NEGATIVE_INFINITY, ev.seq)
    if (ev.type === 'turn/start') {
      open = { frames: [], state: newTurnMapState(), startSeq: ev.seq }
      for (const buffered of pending) {
        open.frames.push(...mapEvent(buffered, open.state))
      }
      pending = []
    }
    if (!open) {
      pending.push(ev)
      continue
    }
    open.frames.push(...mapEvent(ev, open.state))
    if (ev.type === 'turn/end') {
      // After mapEvent's own output (the verbatim turn/end relay + the halt
      // mint when the reason mints one) — the terminal always orders last.
      if (terminals) open.frames.push({
        type: 'turn_closed', seq: ev.seq + TURN_CLOSED_OFFSET,
      })
      turns.push({ frames: open.frames, end_seq: ev.seq })
      open = null
    }
  }
  // INVARIANT(replay-split): the still-open turn is EMITTED (frames verbatim,
  // no end_seq) and the projection never states death — a log cannot tell a
  // mid-turn read (the turn is alive) from a driver crash (it is dead), so it
  // must not render either verdict. The halt-vs-open split is the consumer's:
  // Lore's reload keys rows by `end_seq` (backend/routes/chat/messages.py,
  // _assign_frames_by_stamp), so an open turn pairs with NO row and a halted
  // row's `lore/halt` mint (whose qualifier is "frames is None") stays that
  // turn's ONE record; a resync consumer instead assembles the open turn by
  // seq and renders it live. Why: emitting the open turn AND letting the halt
  // mint land on the same turn would render two records for one dead turn.
  if (open) turns.push({
    frames: open.frames,
    // The gate: only an attempt that began inside THIS open turn seats —
    // startedAfterSeq at or above the turn's own start (see the param doc).
    ...(assistantStream?.activeAttempt !== undefined
      && open.startSeq !== undefined
      && assistantStream.activeAttempt.startedAfterSeq >= open.startSeq
      ? { assistant_stream: assistantStream }
      : {}),
  })
  if (sinceSeq === undefined) return { turns, tail_seq: tail }
  return {
    turns: turns
      .map((t) => ({
        ...t,
        // Every frame today anchors a numeric seq (dsh_event = its event's,
        // a mint = its fractional anchor); one that did not could not be
        // ordered against the boundary, so under resync it is not replayed.
        frames: t.frames.filter((f) => {
          const seq = (f as { seq?: unknown }).seq
          return typeof seq === 'number' && seq > sinceSeq
        }),
      }))
      .filter((t) => t.frames.length > 0 || t.end_seq === undefined),
    tail_seq: tail,
  }
}
