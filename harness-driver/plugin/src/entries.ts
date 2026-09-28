/**
 * Pure replay projection: a session's dsh event log → per-turn frame lists.
 *
 * # SYSTEM: harness-driver (replay half) — the RELOAD input of the one
 *   projection. The frames this returns are the SAME frames the live listener
 *   streamed (map.ts's verbatim `dsh_event` relay + the `lore/verdict-ask`
 *   mint), grouped by turn, so a reload and the live stream agree by
 *   construction rather than by two synchronized copies of the timeline.
 *   PURE — no ctx, no http (test/entries.test.ts).
 *
 * # ARCH: the driving session's log is the ONLY input. A child (subagent)
 *   session keeps its own log, so its nested chips were live-only; the driving
 *   turn's own dispatch replays. Why: inspect() takes one session id, and
 *   walking children would need the child ids the driving log does not record.
 *
 * # ARCH: the replay does not fold the log into a step timeline —
 *   it emits the events VERBATIM (toRelayFrame, the same builder map.ts uses)
 *   and the browser assembles the conversation from them. The only mint is
 *   `lore/verdict-ask` (the one lore fact anchored inside a dsh session event).
 *   The other three lore facts are minted Lore-side on reload: the backend's
 *   timeline attach mints `lore/image-gen` from the row's gen_steps,
 *   `lore/halt` from the row's halt column, and `lore/compaction-mint` from
 *   the compaction/end frame's mint outcome.
 */

import { mapEvent, newTurnMapState, type DshEvent } from './map.ts'

/** One replayed turn: the frames the live stream sent, in log order, plus
 * the dsh log seq of its closing `turn/end` — the SAME id the backend stamps
 * on the turn's assistant row (messages.driver_seq), so the read path keys
 * rows to turns by stamp instead of position (a fork re-seeds the log to the
 * active lineage, breaking any positional pairing). `end_seq` ABSENT marks
 * an OPEN turn (no closing row — a mid-turn replay): the resync primitive;
 * which record a session renders for it, this open turn or the halt card, is
 * the consumer's split (the replay-split INVARIANT below). */
export interface ReplayedTurn {
  frames: Record<string, unknown>[]
  end_seq?: number
}

/** The whole session-entries reply: the turns plus the log's tail seq — the
 * anchor a turn-less lore mint uses when no terminal frame exists (the
 * backend's reload `lore/halt` mint for an abnormally ended turn). */
export interface ReplayedSession {
  turns: ReplayedTurn[]
  tail_seq: number | null
}

/**
 * Group a session log into turns and map each turn's events to relay frames.
 *
 * A turn is `turn/start` … `turn/end`. Events OUTSIDE any turn — a fork seed's
 * leading rows, the session-level bookkeeping dsh writes between turns — buffer
 * into the next turn that opens, so nothing is dropped for sitting BETWEEN two
 * turns. Events sitting after the LAST `turn/end` get no turn to flush into and
 * are NOT replayed, although the live listener relayed them. That is harmless
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
 * frames the live listener relayed above that seq — a resync is a subsequence
 * of the full replay, not a re-derivation. A closed turn left with no
 * surviving frames drops (the consumer already holds it whole, `turn/end`
 * included); the OPEN turn never drops — zero new frames still answers "the
 * turn is open". `tail_seq` stays the WHOLE log's tail regardless: it anchors
 * the reload halt mint and the log's high-water mark, neither of which is a
 * frame.
 */
export function projectSessionEntries(
  events: DshEvent[], sinceSeq?: number,
): ReplayedSession {
  const turns: ReplayedTurn[] = []
  let pending: DshEvent[] = []
  let open: { frames: Record<string, unknown>[]; state: ReturnType<typeof newTurnMapState> } | null = null
  let tail: number | null = null

  for (const ev of events) {
    if (typeof ev.seq === 'number') tail = Math.max(tail ?? Number.NEGATIVE_INFINITY, ev.seq)
    if (ev.type === 'turn/start') {
      open = { frames: [], state: newTurnMapState() }
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
  // mint land on the same turn would render two records for one dead turn
  // (plan agent-line-harness-lifecycle step 1).
  if (open) turns.push({ frames: open.frames })
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
