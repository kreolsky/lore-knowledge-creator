/** The steps every load of a session's rows runs, whichever path fetched them:
 * GET messages (loadMessages) or the sessions piggyback (restore_saved). */
import { recordHistoryLoad } from '../../telemetry/perf';
import { replaceWindowFromRows } from './conversation-feed';
import { adoptOpenTurn } from './streaming';
import type { ChatMessage } from '../../types';
import type { Set, Get } from './types';

// INVARIANT: both load paths go through these two functions, never a hand copy.
// Why: the piggyback branch skipped loadMessages and mirrored its hooks by
// hand, and each step it missed was a reload bug (no pending-verdict card; an
// open turn rendered settled with every live frame dropped).

/** The raw rows' half — BEFORE hydration drops `frames`: the assembler window
 * (SYSTEM: dsh-conversation), then the ADOPTION — an open_turn row re-seats
 * the streaming slot at the boundary the window just published and registers
 * the sink, so a reload mid-turn CONTINUES streaming. */
export function seatSessionRows(get: Get, set: Set, sessionId: string, rows: ChatMessage[]): void {
  replaceWindowFromRows(rows, sessionId, set);
  adoptOpenTurn(get, set, sessionId, rows);
}

/** After the rows are the active session's messages: the history-size
 * tripwire, and the pending verdicts — a still-held call has no live card
 * after a reload (the driver parks on the held POST), so the decision store
 * restores the card onto its assistant message. */
export function afterSessionRowsCommitted(get: Get, sessionId: string, count: number): void {
  recordHistoryLoad(sessionId, count);
  void get().fetchPendingVerdicts(sessionId);
}
