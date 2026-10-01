/**
 * Lore's own conversation-node definitions — the cards dsh has no node for.
 *
 * # SYSTEM: dsh-conversation. The
 * four Lore-minted facts (lore-events.ts) enter the SAME registry dsh's
 * definitions live in, through this module — never by editing what dsh
 * registers (register.ts is vendored upstream). Each definition is the
 * dsh-shape state machine: match on one lore event kind, start from that
 * single event, and materialize one visible chat node anchored at the
 * event's own seq (the producer-minted anchor+offset placement — see the
 * placement INVARIANT in lore-events.ts).
 *
 * The node data is the fact WITHOUT the placement coordinate: `turn` is the
 * assembler's location input, and the node carries placement through its own
 * `location`/`anchorSeq` fields. The renderer reads the data and recomputes
 * nothing (the one rule that keeps a second translation layer from growing
 * back).
 */

import type { Context } from '@deepseek-ai/cordis'
import type {
  ConversationNodeContext, ConversationNodeDefinition,
} from '@deepseek-ai/dsh-client-ui-conversation/client'
// Relative into the workspace, React-free like the register.ts import in
// index.ts — the chatNode helper is the ONE node builder, shared with dsh's
// own definitions.
import { chatNode } from '../../packages/client/ui-chat/src/client/conversation-nodes/common.ts'
import type {
  LoreCompactionMintEventData, LoreHaltEventData, LoreImageGenEventData,
  LoreVerdictAskEventData,
} from './lore-events.ts'

// Relative into the workspace — the declaring module of ChatNodeDataMap.
declare module '../../packages/client/ui-chat/src/client/contract/chat-nodes.ts' {
  interface ChatNodeDataMap {
    /** One detached image generation's settled outcome, at its dispatching call. */
    'image-gen': ImageGenChatData
    /** One mid-turn mutating call asking the user's verdict. */
    'verdict-ask': VerdictAskChatData
    /** What stopped a turn and how far it got. */
    'halt': HaltChatData
    /** Whether the backend minted the continuation chat row for one compaction. */
    'compaction-mint': CompactionMintChatData
  }
}

/** The image chips' render payload. A run ARRIVES as several events (the
 * coarse phases, then one settled event) — the definition's state is the
 * LATEST event's fact: `running` + `phase` while the detached generation
 * runs, then the settled `done` (refiner outcome + prompt, the produced
 * reference ids for thumbnails + lightbox, the target document title) or
 * `failed` (the failure detail) payload. */
export interface ImageGenChatData {
  readonly status: 'running' | 'done' | 'failed'
  readonly runId: string
  readonly phase?: string
  readonly imageRefIds?: readonly string[]
  readonly title?: string
  readonly refine?: { readonly ok: boolean; readonly prompt?: string; readonly error?: string }
  readonly error?: string
}

/** The verdict card's render payload: the parked mutating call. The card
 * stays once asked — the resolved call's own tool row renders below it. */
export interface VerdictAskChatData {
  readonly callId: string
  readonly toolName: string
}

/** The halt card's render payload: what stopped the turn and how far it got.
 * An untyped `reason` renders degraded-but-visible, never silence. */
export interface HaltChatData {
  readonly reason: string
  readonly message?: string
  readonly steps?: number
  readonly limit?: number
}

/** The compaction mint outcome: whether the continuation chat row exists for
 * this compaction window, and the context-occupation signal. */
export interface CompactionMintChatData {
  readonly compactionEntryId: string
  readonly mintFailed: boolean
  readonly mintReason?: string
  readonly tokensBefore?: number
}

/** Strip the placement coordinate; the rest is the node's render payload. */
function factOf<T extends { readonly turn: number | null }>(data: T): Omit<T, 'turn'> {
  const { turn: _turn, ...fact } = data
  return fact as Omit<T, 'turn'>
}

/** One detached image generation. Identity: the run id — one card per run,
 * fed by SEVERAL events (every `running` phase, then the settled event).
 * The first event opens the context (the reload path opens it with the
 * settled event alone); every later event of the same run REPLACES the state
 * with its own fact, so the card always renders the run's LATEST event —
 * the phase climbs, then the settled look folds in. */
export const imageGenDefinition: ConversationNodeDefinition<ImageGenChatData> = {
  kind: 'image-gen',
  target: 'chat',
  match: event => event.type === 'lore/image-gen'
    ? { id: event.data.runId, role: 'start' }
    : null,
  start: (_context, match) => factOf(match.event.data as LoreImageGenEventData),
  update: (_context, match) => factOf(match.event.data as LoreImageGenEventData),
  buildViewNode: (context: ConversationNodeContext<ImageGenChatData>) =>
    context.state === undefined
      ? null
      : chatNode(context, 'image-gen', anchor(context), context.state),
}

/** One mid-turn verdict ask. Identity: the asked call — one card per call. */
export const verdictAskDefinition: ConversationNodeDefinition<VerdictAskChatData> = {
  kind: 'verdict-ask',
  target: 'chat',
  match: event => event.type === 'lore/verdict-ask'
    ? { id: event.data.callId, role: 'start' }
    : null,
  start: (_context, match) => factOf(match.event.data as LoreVerdictAskEventData),
  update: context => context.state,
  buildViewNode: (context: ConversationNodeContext<VerdictAskChatData>) =>
    context.state === undefined
      ? null
      : chatNode(context, 'verdict-ask', anchor(context), context.state),
}

/** One turn-ending halt. Identity: the turn — a turn ends once; a turn-less
 * mint (no dsh anchor exists) falls back to the event's own seq. */
export const haltDefinition: ConversationNodeDefinition<HaltChatData> = {
  kind: 'halt',
  target: 'chat',
  match: event => {
    if (event.type !== 'lore/halt') return null
    return {
      id: event.data.turn === null ? `seq:${event.seq}` : `turn:${event.data.turn}`,
      role: 'start',
    }
  },
  start: (_context, match) => factOf(match.event.data as LoreHaltEventData),
  update: context => context.state,
  buildViewNode: (context: ConversationNodeContext<HaltChatData>) =>
    context.state === undefined
      ? null
      : chatNode(context, 'halt', anchor(context), context.state),
}

/** One compaction's mint outcome. Identity: the compaction window id — dsh's
 * compactionId, stable per compaction, so a replayed mint keys the same fact
 * the backend deduped on. */
export const compactionMintDefinition: ConversationNodeDefinition<CompactionMintChatData> = {
  kind: 'compaction-mint',
  target: 'chat',
  match: event => event.type === 'lore/compaction-mint'
    ? { id: event.data.compactionEntryId, role: 'start' }
    : null,
  start: (_context, match) => factOf(match.event.data as LoreCompactionMintEventData),
  update: context => context.state,
  buildViewNode: (context: ConversationNodeContext<CompactionMintChatData>) =>
    context.state === undefined
      ? null
      : chatNode(context, 'compaction-mint', anchor(context), context.state),
}

function anchor(context: ConversationNodeContext<unknown>): number {
  return context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0
}

/**
 * Register the four Lore-owned conversation Definitions.
 * @param ctx - the bare conversation context the bundle built.
 */
export function registerLoreNodes(ctx: Context): void {
  ctx.uiConversation.events.register(imageGenDefinition)
  ctx.uiConversation.events.register(verdictAskDefinition)
  ctx.uiConversation.events.register(haltDefinition)
  ctx.uiConversation.events.register(compactionMintDefinition)
}
