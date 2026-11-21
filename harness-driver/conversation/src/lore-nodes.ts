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
 * placement INVARIANT in lore-events.ts). A fact dsh's log already states
 * but dsh renders no node for is matched on dsh's own events instead — no
 * lore event is minted for it (the interrupted reply).
 *
 * The node data is the fact WITHOUT the placement coordinate: `turn` is the
 * assembler's location input, and the node carries placement through its own
 * `location`/`anchorSeq` fields. The renderer reads the data and recomputes
 * nothing (the one rule that keeps a second translation layer from growing
 * back).
 */

import type { Context } from '@deepseek-ai/cordis'
import type {
  ConversationMatch, ConversationNodeContext, ConversationNodeDefinition,
} from '@deepseek-ai/dsh-client-ui-conversation/client'
// Relative into the workspace, React-free like the register.ts import in
// index.ts — the chatNode helper is the ONE node builder, shared with dsh's
// own definitions.
import { chatNode } from '../../packages/client/ui-chat/src/client/conversation-nodes/common.ts'
// Relative into the workspace, the same way index.ts reaches expandAssistantStream.
import { assembleAssistantStream } from '../../packages/llm/llm/src/assistant-stream.ts'
import { isAppendSurfaceEvent } from '../../packages/core/session/src/surface.ts'
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
    /** The text a failed model attempt had already streamed. */
    'interrupted-reply': InterruptedReplyChatData
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
  /** The held call's raw args, off its own `tool/call` (dsh appends the call
   * BEFORE it asks — the ask frame carries no args). */
  readonly argsRaw?: string
  /** The call settled: its `tool/result` arrived. A bare `tool/call` is NOT a
   * decision — the call exists before the ask is answered. */
  readonly decided: boolean
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

/** The interrupted reply's render payload: the text blocks the failed
 * attempt streamed, joined; its reasoning is dropped. */
export interface InterruptedReplyChatData {
  readonly text: string
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

/** One call's verdict context: the ask, once it arrived, plus what the call's
 * own tool events say. `ask` stays undefined for a call nobody asked about. */
interface VerdictAskState {
  readonly ask?: Omit<LoreVerdictAskEventData, 'turn'>
  /** The ask's own seq — the card's anchor (the producer-minted placement),
   * not the call's start. */
  readonly askSeq?: number
  readonly argsRaw?: string
  readonly decided: boolean
}

function foldVerdictCall(state: VerdictAskState, match: ConversationMatch): VerdictAskState {
  const event = match.event
  if (event.type === 'lore/verdict-ask') return { ...state, ask: factOf(event.data as LoreVerdictAskEventData), askSeq: event.seq }
  if (event.type === 'tool/call') return { ...state, argsRaw: String(event.data.arguments) }
  // INVARIANT: the ask is decided only once the call's node carries a
  // tool-result — a bare tool-call node is NOT a decision.
  // Why: dsh appends `tool/call` BEFORE it asks for approval, so the running
  // node already exists when the verdict-ask arrives; keying on its presence
  // rendered every live ask as the settled card, with no buttons to answer it.
  if (event.type === 'tool/result') return state.decided ? state : { ...state, decided: true }
  return state
}

/** One mid-turn verdict ask. Identity: the asked call — one card per call.
 * The call's own `tool/call` opens the context (dsh appends it BEFORE asking,
 * and a context's first match must be its start); the ask and the call's
 * `tool/result` fold in, so the card publishes its args and whether it was
 * decided. A call nobody asked about keeps a context but builds no node. */
export const verdictAskDefinition: ConversationNodeDefinition<VerdictAskState> = {
  kind: 'verdict-ask',
  target: 'chat',
  match: (event) => {
    if (event.type === 'lore/verdict-ask') return { id: event.data.callId, role: 'start' }
    if (event.type === 'tool/call') return { id: String(event.data.callId), role: 'start' }
    // The same append-surface guard dsh's tool definition matches results by.
    if (event.type === 'tool/result' && isAppendSurfaceEvent(event)) {
      return { id: String(event.data.message.source.callId), role: 'update' }
    }
    return null
  },
  start: (_context, match) => foldVerdictCall({ decided: false }, match),
  update: (context, match) => foldVerdictCall(context.state, match),
  buildViewNode: (context: ConversationNodeContext<VerdictAskState>) => {
    const state = context.state
    if (state?.ask === undefined || state.askSeq === undefined) return null
    const data: VerdictAskChatData = {
      ...state.ask, decided: state.decided,
      ...(state.argsRaw === undefined ? {} : { argsRaw: state.argsRaw }),
    }
    return chatNode(context, 'verdict-ask', state.askSeq, data)
  },
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

function attemptText(stream: unknown): string {
  if (!Array.isArray(stream)) return ''
  return assembleAssistantStream(stream).interruptedBlocks()
    .flatMap(block => block.type === 'text' ? [block.text] : [])
    .join('\n\n')
}

/** One model attempt cut off by a failed stream. dsh logs the partial stream
 * as `assistant/attempt` and deliberately renders no chat node for it, so the
 * typed text would vanish at the error. Identity: the attempt event itself —
 * one node per failed attempt, started by that single event and located at
 * its step. A cancel settles an `interrupted` message instead, which dsh
 * renders — no attempt, no node here. */
export const interruptedReplyDefinition: ConversationNodeDefinition<InterruptedReplyChatData> = {
  kind: 'interrupted-reply',
  target: 'chat',
  match: event => event.type === 'assistant/attempt'
    ? { id: `attempt:${event.seq}`, role: 'start' }
    : null,
  start: (_context, match) => ({ text: attemptText(match.event.data.stream) }),
  // INVARIANT: text a failed attempt already printed is NEVER hidden — not by a
  // retry, a settled reply, a Stop or a reload; whatever was output stays.
  // Why: operator ruling — the whole point is that typed text never vanishes.
  update: context => context.state,
  // WHY: the attempt waits for the frame so the stream `end` that follows it
  // retires the transient text first — the default 'immediate' would paint
  // this node beside the still-live chunks.
  publication: () => 'animation-frame',
  buildViewNode: (context: ConversationNodeContext<InterruptedReplyChatData>) =>
    context.state === undefined || context.state.text === ''
      ? null
      : chatNode(context, 'interrupted-reply', anchor(context), context.state),
}

function anchor(context: ConversationNodeContext<unknown>): number {
  return context.start?.event.seq ?? context.matches[0]?.event.seq ?? 0
}

/**
 * Register the Lore-owned conversation Definitions.
 * @param ctx - the bare conversation context the bundle built.
 */
export function registerLoreNodes(ctx: Context): void {
  ctx.uiConversation.events.register(imageGenDefinition)
  ctx.uiConversation.events.register(verdictAskDefinition)
  ctx.uiConversation.events.register(haltDefinition)
  ctx.uiConversation.events.register(compactionMintDefinition)
  ctx.uiConversation.events.register(interruptedReplyDefinition)
}
