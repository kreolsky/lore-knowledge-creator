/**
 * Pure relay: dsh session events → Lore SSE frames, keyed by the dsh `kind`.
 *
 * # SYSTEM: harness-driver (relay half) — the RENTED loop speaking the dsh
 *   vocabulary end to end. The relay NO LONGER TRANSLATES: every dsh event
 *   emits VERBATIM as a
 *   neutral `{type:'dsh_event', kind, seq, time, data, surfaceOp?,
 *   sourceEventSeqs?}` frame — the whole event, no truncation, no hide list,
 *   with exactly TWO declared exceptions: a VALID `approval/asked` emits only
 *   the `lore/verdict-ask` mint (the raw event does not ride — the mint carries
 *   the ask's coordinates and the raw one would render a second time), and the
 *   backend's `_session_title_arm` (backend/driver/frames.py) consumes
 *   `session/title` without relaying it (that arm's own docstring says why) —
 *   and the BROWSER assembles the conversation from these frames (dsh's own
 *   ConversationNodeAssembler; Lore renders React over the node data). Visibility
 *   is the assembler's node definitions' decision, not a relay filter: a kind
 *   without a definition produces no node at all, which is the same hiding,
 *   decided by the same client that renders.
 *
 *   BESIDE the log events, the standing channel carries ONE non-mapEvent
 *   frame (ws-events.ts relayAssistantStream): `{type:'dsh_stream', frame}` —
 *   dsh's own `agent/assistant-stream` publication relayed verbatim (0.1.5:
 *   the v3 log holds only SETTLED events, so the live tail is a transient
 *   publication, never a session event). It never enters mapEvent (no seq, no
 *   mint, no state) and is never replayed — a resync reads the settled log.
 *
 * The ONE exception is the lore mint arm: dsh's log cannot state every fact a
 *   Lore chat shows. What Lore keeps because dsh cannot state it enters as OUR
 *   `lore/*` events in the SAME registry (harness-driver/conversation/src/
 *   lore-events.ts). The relay mints exactly ONE of them, because exactly one
 *   has its anchor inside a dsh session event: `lore/verdict-ask`, on
 *   `approval/asked` (anchor = the ask's own seq). The other three are minted
 *   where their facts live: `lore/image-gen` by the detached-generation
 *   pipeline (outside the dsh log; the browser mints it live off the project
 *   WS, the backend mints it on reload from the row's gen_steps), `lore/halt`
 *   by the backend (a Lore-side fact: disconnect / deadline / driver failure —
 *   plus, on reload, the row's `halt` column), `lore/compaction-mint` by the
 *   backend (the continuation-chat mint outcome is a backend product).
 *
 * The offsets (LORE_SEQ_OFFSETS) and the mint shape come from
 *   lore-events.ts — imported, never copied, so a reload minted from the same
 *   anchor lands at the identical position (the placement INVARIANT there).
 *
 * # ARCH (child sessions): a child (subagent) session's events emit NOTHING.
 *   The nested child chips the old translator built were its own vocabulary,
 *   live-only (the child log is not replayed — entries.ts ARCH note), and a
 *   child seq would collide with the driving session's seq space in the
 *   assembler. Subagent replay stays the named gap (the plan's Not-doing).
 *
 * This module is PURE (no ctx, no http) so it is unit-testable without booting
 *   the harness (test/map.test.ts).
 */

// Image-relative into the conversation package: inside the harness image the
// plugin lives at /dsh/lore-driver and the package at /dsh/lore-conversation,
// both beside the dsh workspace — the same layout this repo keeps under
// harness-driver/. (The plugin compiles only in the image; no local toolchain
// resolves the harness workspace.)
import { loreEvent } from '../../lore-conversation/src/lore-events.ts'


/**
 * One dsh SessionEvent as it arrives off the session feed / a decoded log
 * (shape used read-only). The ONE shared declaration for the relay.
 *
 * # ARCH (shapes verified against a real 0.1.5-rc.2 log): `surfaceOp` is the
 * STRING 'append' or the object {op:'replace', start, end} and rides ONLY on
 * the three surface kinds (user/message, assistant/message, tool/result);
 * `sourceEventSeqs` likewise. A `user/message` carries the message payload as
 * `data` ITSELF (role/content at the top level), while `assistant/message`
 * nests it under `data.message` with the producing model at
 * `data.message.source.model`. `time` is epoch ms — present on every log
 * event; optional here because tests build bare events.
 */
export interface DshEvent {
  seq: number
  type: string
  data?: any
  time?: number
  surfaceOp?: string | { op: 'replace'; start: number; end: number } | undefined
  sourceEventSeqs?: number[]
}

/** Accumulated per-turn state the relay needs across events. */
export interface TurnMapState {
  /** Set when the driving turn's terminal `turn/end` event relayed. */
  finished: boolean
  /** The dsh turn number of the OPEN turn (the lore mints' turn coordinate). */
  turn: number | null
}

export function newTurnMapState(_faults = ''): TurnMapState {
  return { finished: false, turn: null }
}

/** Which session one event belongs to (the listener knows; the event does not). */
export interface EventOrigin {
  /** The emitting dsh session id. */
  sessionId: string
  /** True for any session that is not the driving one (a subagent child). */
  isChild: boolean
}

/**
 * The verbatim relay frame for one dsh event: the event under
 * `kind + seq + time + data`, surface metadata included, nothing derived.
 * The browser reconstructs the SessionEvent from exactly these fields and
 * feeds the assembler — the frame is the event's transport, not a view of it.
 */
export function toRelayFrame(ev: DshEvent): Record<string, unknown> {
  return {
    type: 'dsh_event',
    kind: ev.type,
    seq: ev.seq ?? null,
    ...(typeof ev.time === 'number' ? { time: ev.time } : {}),
    data: ev.data ?? null,
    ...(ev.surfaceOp !== undefined ? { surfaceOp: ev.surfaceOp } : {}),
    ...(ev.sourceEventSeqs !== undefined ? { sourceEventSeqs: ev.sourceEventSeqs } : {}),
  }
}

/** The lore events a turn/end reason mints: the reasons dsh renders NO node
 * for (its turn-error node covers `error`, turn-max-tokens covers
 * `max-tokens`, `completed` needs nothing). An unknown reason mints too —
 * degraded-but-visible, never silence. */
const HALT_MESSAGES: Record<string, string> = {
  aborted: 'The turn was cancelled.',
  blocked: 'The turn was blocked before a model step.',
  interrupted: 'The session was recovered after an interruption.',
}

/**
 * The frames for one event; empty array = nothing to emit. `origin` names the
 * emitting session (the listener knows it; the event does not).
 */
export function mapEvent(
  ev: DshEvent, state: TurnMapState, origin?: EventOrigin,
): Record<string, unknown>[] {
  // A child session's events emit nothing (the module ARCH note): its chips
  // were the translator's own vocabulary, and its seq space would collide
  // with the driving session's inside the assembler.
  if (origin?.isChild) return []
  if (ev.type === 'turn/start') {
    const turn = ev.data?.turn
    state.turn = typeof turn === 'number' ? turn : state.turn
    return [toRelayFrame(ev)]
  }
  if (ev.type === 'turn/end') {
    state.finished = true
    const kind = ev.data?.reason?.kind
    const frames: Record<string, unknown>[] = [toRelayFrame(ev)]
    if (kind !== 'completed' && kind !== 'max-tokens' && kind !== 'error') {
      // A reason dsh renders no node for mints the lore halt card at the
      // turn/end's own seq — the SAME mint the replay emits (same function),
      // so reload parity is by construction and the producer obligation
      // (distinct anchors per kind) is trivially kept: one halt per turn.
      // .event: the wire frame is the FLAT lore event (type/seq/time/data/
      // ignorable) — the same shape the backend mints (driver/client.py's
      // frame contract); loreEvent wraps it for the assembler's input.
      frames.push(loreEvent(
        'lore/halt',
        ev.seq,
        {
          turn: state.turn,
          reason: typeof kind === 'string' ? kind : 'unknown',
          ...(typeof kind === 'string' && HALT_MESSAGES[kind]
            ? { message: HALT_MESSAGES[kind] }
            : {}),
        },
        typeof ev.time === 'number' ? ev.time : Date.now(),
      ).event as unknown as Record<string, unknown>)
    }
    return frames
  }
  if (ev.type === 'approval/asked') {
    // Mid-turn approval, DECLARED by the ask: dsh's user-approval service
    // appended this audit event because a mutating call needs the user's
    // verdict. The ask card is OURS (dsh answers approvals in a composer
    // panel and has no conversation node for the ask), so the relay mints the
    // lore event at the ask's own seq — the SAME anchor the replay mints on
    // reload (the placement INVARIANT in lore-events.ts). The payload carries
    // the turn coordinate; the card keys on the call id.
    const callId = ev.data?.callId
    const name = ev.data?.toolName
    if (typeof callId !== 'string' || !callId || typeof name !== 'string' || !name) {
      // A malformed ask cannot anchor a card — relay it verbatim instead of
      // minting a broken one, so the event still reaches the browser.
      return [toRelayFrame(ev)]
    }
    return [loreEvent(
      'lore/verdict-ask',
      ev.seq,
      { turn: state.turn, callId, toolName: name },
      typeof ev.time === 'number' ? ev.time : Date.now(),
    ).event as unknown as Record<string, unknown>]
  }
  return [toRelayFrame(ev)]
}
