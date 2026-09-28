/**
 * Pure fork machinery: the dsh session event log → turn-boundary resolution.
 *
 * # SYSTEM: harness-driver (leaf half). The branch point is named in the
 *   DRIVER's own id space — the dsh log seq of a `turn/end` row (stamped on
 *   the Lore row when its terminal frame relayed it; /session-leaf takes the
 *   seq directly). PURE — no ctx, no http
 *   (test/driver.test.ts).
 */

import type { DshEvent } from './map.ts'

/**
 * The seq of the last `turn/end` — the live tail's boundary. -1 when the log
 * carries no completed turn.
 *
 * # INVARIANT: /session-leaf at the live tail is a NO-OP. The backend's seam A
 * names the parent turn on EVERY linear turn; without this guard each turn
 * would fork a fresh seeded session, and (a) the harness's pressure compaction
 * never fires on a seeded surface, so the driver contract's `compaction` frame
 * becomes unreachable on linear chats, and (b) in-place history accumulation —
 * what resume exists for — never happens. The no-op test is SEQ EQUALITY
 * against this value (the index-era isLiveTailTurn, rekeyed to the seq).
 */
export function lastTurnEndSeq(events: DshEvent[]): number {
  let last = -1
  for (const ev of events) {
    if (ev.type === 'turn/end' && ev.seq > last) last = ev.seq
  }
  return last
}

/**
 * The log index of the `turn/end` row carrying `seq` — the fork's boundary
 * row (the seed is the POSITIONAL prefix through it). -1 unless `seq` names
 * an EXISTING `turn/end` row: any other value (a stale stamp, a non-boundary
 * row's seq, garbage, a negative) is unresolvable and the endpoint refuses it
 * — a fork must never seed to a boundary it cannot prove.
 */
export function turnEndIndex(events: DshEvent[], seq: number): number {
  if (!Number.isInteger(seq) || seq < 0) return -1
  const i = events.findIndex((ev) => ev.seq === seq)
  return i >= 0 && events[i]!.type === 'turn/end' ? i : -1
}
