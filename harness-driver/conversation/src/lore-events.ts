/**
 * The Lore event vocabulary — the facts dsh's log cannot state.
 *
 * # SYSTEM: dsh-conversation. What Lore keeps because dsh cannot state it:
 * the detached image
 * generation (it runs outside the dsh log, delivered over the project WS),
 * the mid-turn verdict card (dsh answers approvals in a composer panel and
 * has NO conversation node for the ask), the abnormal-end halt (a Lore-side
 * fact: a client disconnect and our deadline breach never reach a dsh log),
 * and the compaction mint outcome (the continuation chat row is a Lore
 * product). Each fact is ONE `lore/*` SessionEvent, minted Lore-side — live
 * stream and reload replay alike — and fed to the SAME assembler as the dsh
 * events; the definitions in lore-nodes.ts render them.
 *
 * The envelope carries `ignorable: true`: a lore event that ever reached a
 * dsh log must stay skippable by a reader that does not know the kind (the
 * SessionEvent.ignorable contract). The events never enter a dsh log
 * themselves — they ride Lore's own stream — but the marker is what keeps
 * that fact harmless if the boundary ever moves.
 *
 * # INVARIANT (placement): an event's `seq` is minted as the anchor dsh
 * event's seq plus the kind's offset from LORE_SEQ_OFFSETS, and the node
 * definitions anchor at `context.start.event.seq` — the engine never
 * recomputes a placement, so a reload minted from the same anchor lands at
 * the identical position. Reload==live parity is therefore the producer's
 * contract: mint the same anchor on both paths, and feed the event through
 * `LoreConversation.splice` — never `append`, whose location index would take
 * a behind-the-tail mint as the new cursor (the splice INVARIANT in
 * index.ts). The payload always carries its `turn` coordinate (null when the
 * fact has no turn) because an out-of-band event must not rely on the
 * location index's live cursor — a later turn-less event would otherwise
 * inherit the lore event's turn. A run's PHASE events (lore/image-gen
 * `running`) are the one sanctioned intra-anchor ladder: their seqs climb
 * strictly from just above the anchor toward the kind's settled offset, so
 * every phase APPENDS into the run's one context and the settled event still
 * lands at the exact offset a reload re-derives.
 *
 * # INVARIANT (identity): one lore context per fact. The definitions key
 * contexts on the fact id (run id, call id, turn, compaction id); a fact's
 * later events (an image run's phases, its settled event) UPDATE the same
 * context, and a re-mint of an already-held seq fails the assembler loudly
 * (duplicate Match) rather than rendering the fact twice.
 */

// Relative into the workspace, the same convention as src/index.ts: this is
// the module that DECLARES SessionEvent, and no mapped specifier for it
// exists outside the full package barrel.
import type { SessionEvent } from '../../packages/core/session/src/types.ts'
import type { SessionLiveEventEntry } from '@deepseek-ai/dsh-api-session-controller/client'

/** The detached image generation's payload. `turn` is the dispatching call's
 * turn — the location coordinate the assembler reads; `null` places the fact
 * at session level. A run arrives as SEVERAL events over its life:
 * `status: 'running'` per coarse phase (carrying
 * `phase`), then ONE settled event (`done`/`failed`) whose payload is the
 * chips' content — the produced reference ids, the target document title,
 * the refiner outcome (which never returns to the agent) and the failure
 * detail. Every event of one run carries the same `runId`; the node
 * definition folds them into one card (see lore-nodes.ts). */
export interface LoreImageGenEventData {
  readonly turn: number | null
  readonly runId: string
  readonly status: 'running' | 'done' | 'failed'
  readonly phase?: string
  readonly imageRefIds?: readonly string[]
  readonly title?: string
  readonly refine?: { readonly ok: boolean; readonly prompt?: string; readonly error?: string }
  readonly error?: string
}

/** The mid-turn approval ask: the mutating call that is parked until the user
 * decides. Identity is the CALL id — the card needs nothing else (the verdict
 * endpoints take call_id + session_id; the assistant row is the renderer's
 * context, not the fact's). */
export interface LoreVerdictAskEventData {
  readonly turn: number | null
  readonly callId: string
  readonly toolName: string
}

/** The halt card's content: the reason (typed HaltReason or an untyped
 * string — rendered degraded-but-visible, never silence), the driver's own
 * sentence, and how far the turn got. */
export interface LoreHaltEventData {
  readonly turn: number | null
  readonly reason: string
  readonly message?: string
  readonly steps?: number
  readonly limit?: number
}

/** The compaction mint outcome (backend/driver/frames.py): whether the
 * continuation chat row was minted for the compaction window. */
export interface LoreCompactionMintEventData {
  readonly turn: number | null
  readonly compactionEntryId: string
  readonly mintFailed: boolean
  readonly mintReason?: string
  readonly tokensBefore?: number
}

/** The lore kind → payload map; the SessionEventMap augmentation below makes
 * these keys first-class SessionEvent types. */
interface LoreEventDataMap {
  'lore/image-gen': LoreImageGenEventData
  'lore/verdict-ask': LoreVerdictAskEventData
  'lore/halt': LoreHaltEventData
  'lore/compaction-mint': LoreCompactionMintEventData
}

/** Relative into the workspace — the declaring module of ChatNodeDataMap's
 * sibling SessionEventMap (core session types). */
declare module '../../packages/core/session/src/types.ts' {
  interface SessionEventMap {
    /** One detached image generation's settled outcome, rendered at its anchor. */
    'lore/image-gen': LoreImageGenEventData
    /** One mid-turn mutating call asking the user's verdict. */
    'lore/verdict-ask': LoreVerdictAskEventData
    /** What stopped a turn and how far it got (graceful halt or abnormal end). */
    'lore/halt': LoreHaltEventData
    /** Whether the backend minted the continuation chat row for one compaction. */
    'lore/compaction-mint': LoreCompactionMintEventData
  }
}

/** The lore event kinds this vocabulary defines. */
export type LoreEventKind = keyof LoreEventDataMap

/**
 * Fractional placement of each lore event after its anchor dsh event's seq.
 * Integers are dsh's; a lore event lives strictly between its anchor event
 * and the next one, and the distinct offsets keep different-kind facts
 * minted at one anchor apart. PROTOCOL: the producers (plugin live stream,
 * backend reload replay) mint with exactly these values — they are what
 * makes a reload land where the live stream did.
 */
export const LORE_SEQ_OFFSETS = {
  verdictAsk: 0.5,
  imageGen: 0.6,
  halt: 0.7,
  compactionMint: 0.8,
} as const

const OFFSET_KEY: Record<LoreEventKind, keyof typeof LORE_SEQ_OFFSETS> = {
  'lore/image-gen': 'imageGen',
  'lore/verdict-ask': 'verdictAsk',
  'lore/halt': 'halt',
  'lore/compaction-mint': 'compactionMint',
}

/**
 * Mint one lore event as an assembler input, placed after its anchor.
 * @param kind - the lore event kind.
 * @param anchorSeq - the dsh event's seq the fact renders at.
 * @param data - the fact payload (carries its own turn coordinate).
 * @param time - epoch ms; the caller's wall clock on the live path, the
 *   persisted frame's time on reload.
 * @returns the SessionLiveEventEntry the assembler consumes.
 */
export function loreEvent<K extends LoreEventKind>(
  kind: K,
  anchorSeq: number,
  data: LoreEventDataMap[K],
  time: number,
): SessionLiveEventEntry {
  const event = {
    type: kind,
    seq: anchorSeq + LORE_SEQ_OFFSETS[OFFSET_KEY[kind]],
    time,
    data,
    ignorable: true,
  } as SessionEvent
  return { type: 'event', event }
}
