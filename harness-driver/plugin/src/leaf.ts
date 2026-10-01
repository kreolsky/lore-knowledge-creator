/**
 * Pure fork machinery: the dsh session event log → the live-tail boundary.
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
 *   names the parent turn on EVERY linear turn; without this guard each turn
 *   would fork a fresh seeded session, and in-place history accumulation —
 *   what resume exists for — never happens. The no-op test is SEQ EQUALITY
 *   against this value.
 */
export function lastTurnEndSeq(events: DshEvent[]): number {
  let last = -1
  for (const ev of events) {
    if (ev.type === 'turn/end' && ev.seq > last) last = ev.seq
  }
  return last
}
